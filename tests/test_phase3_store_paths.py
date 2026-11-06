"""Targeted error and boundary paths for durable storage."""

from __future__ import annotations

import pytest

from delivery_recovery.api.runtime import build_runtime
from delivery_recovery.storage.config import StorageConfig
from delivery_recovery.storage.store import ApplicationStore, QueueFullError, StoreError

from phase3_support import draft


def _config(tmp_path, **overrides):
    values = dict(
        data_dir=tmp_path,
        db_path=tmp_path / "app.db",
        checkpoint_path=tmp_path / "cp.db",
        lease_seconds=30,
        pending_retention_seconds=3600,
    )
    values.update(overrides)
    return StorageConfig(**values)


def _create(store, run_id, hash_value=None, key=None):
    value = draft()
    return store.create_run(
        draft_json=value.model_dump(mode="json"),
        request_hash=hash_value or f"hash-{run_id}",
        run_id=run_id,
        thread_id=run_id,
        principal_id="planner.demo",
        idempotency_key=key or f"key-{run_id}",
    )


def test_create_idempotency_same_content_and_conflict(tmp_path):
    store = ApplicationStore(_config(tmp_path))
    first, created = _create(store, "run-1", "same", "key-1")
    second, replayed = _create(store, "run-2", "same", "key-1")
    assert created is True
    assert replayed is False
    assert first["run_id"] == second["run_id"]
    with pytest.raises(StoreError) as error:
        _create(store, "run-3", "different", "key-1")
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_empty_claim_heartbeat_and_expiry_paths(tmp_path):
    store = ApplicationStore(_config(tmp_path))
    assert store.claim_next("worker") is None
    assert store.expire_pending() == 0
    assert store.recover_expired_leases() == 0
    assert store.heartbeat("missing", "worker") is False
    store.worker_heartbeat("worker")
    assert store.worker_ready("worker") is True
    assert store.worker_ready("missing") is False


def test_enqueue_resume_respects_queue_capacity(tmp_path):
    store = ApplicationStore(_config(tmp_path, max_queued_jobs=4))
    for index in range(4):
        _create(store, f"run-{index}")
    with pytest.raises(QueueFullError):
        store.enqueue_resume("run-0", kind="clarification", payload={"answer": 1})


def test_invalid_waiting_status_is_rejected(tmp_path):
    store = ApplicationStore(_config(tmp_path))
    with pytest.raises(ValueError):
        store.mark_waiting("missing", status="completed", pending=None)


def test_pending_retention_limit_is_enforced(tmp_path):
    config = _config(tmp_path, max_pending_human=1)
    runtime = build_runtime(config, start_worker=False)
    try:
        missing = draft(target_finish_offset_workdays=None)
        runtime.store.create_run(
            draft_json=missing.model_dump(mode="json"),
            request_hash="missing-a",
            run_id="run-a",
            thread_id="run-a",
            principal_id="planner.demo",
            idempotency_key="a",
        )
        assert runtime.worker.run_once()["status"] == "needs_input"
        runtime.store.create_run(
            draft_json=missing.model_dump(mode="json"),
            request_hash="missing-b",
            run_id="run-b",
            thread_id="run-b",
            principal_id="planner.demo",
            idempotency_key="b",
        )
        result = runtime.worker.run_once()
        assert result["status"] == "failed"
        assert runtime.store.get_run("run-b")["status"] == "failed"
    finally:
        runtime.close()


def test_commit_decision_error_paths(tmp_path):
    runtime = build_runtime(_config(tmp_path), start_worker=False)
    try:
        with pytest.raises(StoreError) as missing_run:
            runtime.store.commit_decision(
                "missing",
                decision_id="d",
                revision_id=None,
                candidate_id="C",
                project_version=1,
                input_hash="i",
                catalogue_hash="c",
                approved=False,
                actor_id="m",
                actor_role="manager",
                idempotency_key="k",
                request_hash="r",
                revised_plan=None,
            )
        assert missing_run.value.code == "RUN_NOT_FOUND"

        _create(runtime.store, "run-pending")
        with pytest.raises(StoreError) as wrong_status:
            runtime.store.commit_decision(
                "run-pending",
                decision_id="d",
                revision_id=None,
                candidate_id="C",
                project_version=1,
                input_hash="i",
                catalogue_hash="c",
                approved=False,
                actor_id="m",
                actor_role="manager",
                idempotency_key="k",
                request_hash="r",
                revised_plan=None,
            )
        assert wrong_status.value.code == "DECISION_CONFLICT"
    finally:
        runtime.close()


def test_complete_run_persists_error_and_audit_helpers(tmp_path):
    runtime = build_runtime(_config(tmp_path), start_worker=False)
    try:
        _create(runtime.store, "run-error")
        runtime.store.fail_run("run-error", code="SYNTHETIC", message="failed")
        record = runtime.store.get_run("run-error")
        assert record["status"] == "failed"
        assert record["error"]["code"] == "SYNTHETIC"
        runtime.store.add_audit(
            "run-error", "MANUAL_NOTE", actor_id="tester", payload={"safe": True}
        )
        events = runtime.store.list_audit("run-error")
        assert any(event["event_type"] == "MANUAL_NOTE" for event in events)
        assert runtime.store.list_revisions("run-error") == []
    finally:
        runtime.close()


def test_store_idempotency_lookup_and_direct_commit_replay(tmp_path):
    runtime = build_runtime(_config(tmp_path), start_worker=False)
    try:
        _create(runtime.store, "run-replay", "hash", "lookup-key")
        assert runtime.store.find_by_idempotency(
            "planner.demo", "lookup-key"
        )["run_id"] == "run-replay"
        assert runtime.store.find_by_idempotency("planner.demo", "missing-key") is None
    finally:
        runtime.close()


def test_direct_commit_replay_and_missing_revision(tmp_path):
    runtime = build_runtime(_config(tmp_path), start_worker=False)
    try:
        _create(runtime.store, "run-direct")
        # Drive to awaiting approval with a normal worker run.
        runtime.worker.run_once()
        run = runtime.store.get_run("run-direct")
        binding = run["pending_decision"]
        project_state = {
            "project_id": run["project_id"],
            "version": binding["project_version"],
            "input_hash": binding["input_hash"],
            "catalogue_hash": binding["catalogue_hash"],
            "project_json": draft().to_project_input().model_dump(mode="json"),
        }
        args = dict(
            decision_id="direct-decision",
            revision_id=None,
            candidate_id=binding["candidate_id"],
            project_version=binding["project_version"],
            input_hash=binding["input_hash"],
            catalogue_hash=binding["catalogue_hash"],
            approved=False,
            actor_id="manager.demo",
            actor_role="manager",
            idempotency_key="direct-key",
            request_hash="direct-request",
            revised_plan=None,
            current_project_state=project_state,
        )
        first = runtime.store.commit_decision("run-direct", **args)
        second = runtime.store.commit_decision("run-direct", **args)
        assert first["decision_id"] == second["decision_id"]

        _create(runtime.store, "run-approved")
        runtime.worker.run_once()  # resumes/completes run-direct
        runtime.worker.run_once()  # drives run-approved to approval
        binding = runtime.store.get_run("run-approved")["pending_decision"]
        with pytest.raises(StoreError) as invalid:
            runtime.store.commit_decision(
                "run-approved",
                decision_id="bad-approved",
                revision_id=None,
                candidate_id=binding["candidate_id"],
                project_version=binding["project_version"],
                input_hash=binding["input_hash"],
                catalogue_hash=binding["catalogue_hash"],
                approved=True,
                actor_id="manager.demo",
                actor_role="manager",
                idempotency_key="bad-approved-key",
                request_hash="bad-approved-request",
                revised_plan=None,
                current_project_state={
                    "project_id": "UTILITY-DEMO-001",
                    "version": binding["project_version"],
                    "input_hash": binding["input_hash"],
                    "catalogue_hash": binding["catalogue_hash"],
                    "project_json": draft().to_project_input().model_dump(mode="json"),
                },
            )
        assert invalid.value.code == "DECISION_INVALID"
    finally:
        runtime.close()


def test_ready_false_for_wrong_schema_and_naive_worker_heartbeat(tmp_path):
    from datetime import datetime

    runtime = build_runtime(_config(tmp_path), start_worker=False)
    try:
        connection = runtime.store._connect()
        connection.execute("DELETE FROM schema_migrations")
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (99, 'test')"
        )
        connection.close()
        assert runtime.store.ready() is False
    finally:
        runtime.close()

    rebuilt = build_runtime(_config(tmp_path), start_worker=False)
    try:
        # Restore schema version for the readiness path.
        connection = rebuilt.store._connect()
        connection.execute("DELETE FROM schema_migrations")
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (2, 'test')"
        )
        connection.execute(
            "DELETE FROM worker_heartbeats WHERE worker_id = 'naive'"
        )
        connection.execute(
            "INSERT INTO worker_heartbeats(worker_id, started_at, heartbeat_at) "
            "VALUES ('naive', ?, ?)",
            (datetime.now().isoformat(), datetime.now().isoformat()),
        )
        connection.close()
        assert rebuilt.store.worker_ready("naive") is True
    finally:
        rebuilt.close()
