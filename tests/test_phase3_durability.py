"""Phase 3A durable queue, lease and human-wait regressions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from delivery_recovery.storage.config import StorageConfig
from delivery_recovery.storage.db import migrate, utc_now
from delivery_recovery.storage.store import ApplicationStore, QueueFullError

from phase3_support import build_test_runtime, draft


def _create(store: ApplicationStore, run_id: str, *, draft_value=None):
    value = draft_value or draft()
    return store.create_run(
        draft_json=value.model_dump(mode="json"),
        request_hash=f"hash-{run_id}",
        run_id=run_id,
        thread_id=run_id,
        principal_id="planner.demo",
        idempotency_key=f"create-{run_id}",
    )


def test_migrations_are_idempotent_and_configurable(tmp_path):
    config = StorageConfig(
        data_dir=tmp_path,
        db_path=tmp_path / "application.db",
        checkpoint_path=tmp_path / "checkpoints.db",
    )
    assert migrate(config.db_path) == 2
    assert migrate(config.db_path) == 2
    store = ApplicationStore(config)
    assert store.schema_version == 2
    assert store.ready() is True


def test_queued_jobs_are_persisted_before_acceptance(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        run, created = _create(runtime.store, "run-persisted")
        assert created is True
        assert run["status"] == "queued"
        reopened = ApplicationStore(runtime.config)
        assert reopened.get_run("run-persisted")["status"] == "queued"
        assert reopened.get_job("run-persisted")["state"] == "queued"
    finally:
        runtime.close()


def test_one_active_and_four_queued_jobs_are_enforced(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        for index in range(4):
            _create(runtime.store, f"run-{index}")
        with pytest.raises(QueueFullError):
            _create(runtime.store, "run-overflow")
        assert runtime.store.get_run("run-overflow") is None
    finally:
        runtime.close()


def test_expired_lease_is_reclaimed_after_restart(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        _create(runtime.store, "run-lease")
        base = utc_now()
        claimed = runtime.store.claim_next("worker-old", now=base)
        assert claimed is not None
        assert claimed["job"]["lease_owner"] == "worker-old"
        later = (
            datetime.fromisoformat(base) + timedelta(seconds=31)
        ).isoformat()
        reopened = ApplicationStore(runtime.config)
        reclaimed = reopened.claim_next("worker-new", now=later)
        assert reclaimed is not None
        assert reclaimed["run"]["run_id"] == "run-lease"
        assert reclaimed["job"]["lease_owner"] == "worker-new"
        assert reclaimed["job"]["attempts"] == 2
    finally:
        runtime.close()


def test_pending_human_input_survives_and_does_not_block_other_jobs(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        missing = draft(target_finish_offset_workdays=None, budget_pence=None)
        _create(runtime.store, "run-waiting", draft_value=missing)
        assert runtime.worker.run_once()["status"] == "needs_input"
        waiting = runtime.store.get_run("run-waiting")
        assert waiting["status"] == "needs_input"
        assert waiting["retention_expires_at"] is not None

        _create(runtime.store, "run-runnable")
        assert runtime.worker.run_once()["status"] == "awaiting_approval"
        assert runtime.store.get_run("run-runnable")["status"] == "awaiting_approval"

        reopened = ApplicationStore(runtime.config)
        assert reopened.get_run("run-waiting")["status"] == "needs_input"
        assert reopened.get_job("run-waiting")["state"] == "needs_input"
        assert reopened.get_run("run-runnable")["status"] == "awaiting_approval"
    finally:
        runtime.close()


def test_pending_retention_expires_separately_from_runnable_queue(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        missing = draft(target_finish_offset_workdays=None)
        _create(runtime.store, "run-expire", draft_value=missing)
        runtime.worker.run_once()
        waiting = runtime.store.get_run("run-expire")
        expiry = waiting["retention_expires_at"]
        later = (
            datetime.fromisoformat(expiry) + timedelta(seconds=1)
        ).isoformat()
        assert runtime.store.expire_pending(now=later) == 1
        assert runtime.store.get_run("run-expire")["status"] == "expired"
        assert runtime.store.get_job("run-expire")["state"] == "expired"
    finally:
        runtime.close()


def test_clarification_resumes_from_persistent_checkpoint_after_runtime_rebuild(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        missing = draft(target_finish_offset_workdays=None, budget_pence=None)
        _create(runtime.store, "run-clarify", draft_value=missing)
        assert runtime.worker.run_once()["status"] == "needs_input"
        first = runtime.store.get_run("run-clarify")
        pending = first["pending_question"]
        runtime.store.enqueue_resume(
            "run-clarify",
            kind="clarification",
            payload={"field": pending["field"], "answer": 15},
        )
    finally:
        runtime.close()

    rebuilt = build_test_runtime(tmp_path)
    try:
        result = rebuilt.worker.run_once()
        assert result is not None
        assert result["status"] == "needs_input"
        second = rebuilt.store.get_run("run-clarify")
        assert second["run_id"] == first["run_id"]
        assert second["thread_id"] == first["thread_id"]
        assert second["pending_question"]["field"] == "budget_pence"
    finally:
        rebuilt.close()


def test_only_one_job_is_leased_as_active(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        _create(runtime.store, "run-active-1")
        _create(runtime.store, "run-active-2")
        first = runtime.store.claim_next("worker-one")
        assert first is not None
        second = runtime.store.claim_next("worker-two")
        assert second is None
        connection = runtime.store._connect()
        active = connection.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE state = 'running'"
        ).fetchone()["count"]
        connection.close()
        assert active == 1
    finally:
        runtime.close()
