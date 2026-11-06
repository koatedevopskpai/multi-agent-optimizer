"""Phase 3B authorisation, decision transaction and crash-window tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from delivery_recovery.approvals import DecisionRequest
from delivery_recovery.candidates import evaluate_recovery
from delivery_recovery.storage.store import StoreError

from phase3_support import build_test_runtime, draft


def _awaiting_run(runtime, run_id: str = "run-decision"):
    value = draft()
    runtime.store.create_run(
        draft_json=value.model_dump(mode="json"),
        request_hash=f"hash-{run_id}",
        run_id=run_id,
        thread_id=run_id,
        principal_id="planner.demo",
        idempotency_key=f"create-{run_id}",
    )
    result = runtime.worker.run_once()
    assert result["status"] == "awaiting_approval"
    return runtime.store.get_run(run_id)


def _manager(runtime):
    return next(
        identity.principal
        for identity in runtime.identity.identities
        if identity.principal.is_manager()
    )


def _planner(runtime):
    return next(
        identity.principal
        for identity in runtime.identity.identities
        if identity.principal.is_planner()
    )


def _request(run) -> DecisionRequest:
    binding = run["pending_decision"]
    return DecisionRequest(
        candidate_id=binding["candidate_id"],
        project_version=binding["project_version"],
        input_hash=binding["input_hash"],
        catalogue_hash=binding["catalogue_hash"],
        approved=True,
    )


def test_only_manager_can_approve_and_wrong_project_is_denied(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        run = _awaiting_run(runtime)
        request = _request(run)
        with pytest.raises(StoreError) as denied:
            runtime.decisions.submit(
                run["run_id"],
                request,
                principal=_planner(runtime),
                idempotency_key="planner-key",
            )
        assert denied.value.code == "FORBIDDEN"

        wrong_project = _manager(runtime).model_copy(
            update={"project_ids": ("OTHER-PROJECT",)}
        )
        with pytest.raises(StoreError) as denied_project:
            runtime.decisions.submit(
                run["run_id"],
                request,
                principal=wrong_project,
                idempotency_key="wrong-project-key",
            )
        assert denied_project.value.code == "FORBIDDEN"
        assert runtime.store.get_decision(run["run_id"]) is None
    finally:
        runtime.close()


def test_stale_version_or_hash_is_rejected(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        run = _awaiting_run(runtime)
        request = _request(run)
        for stale in (
            request.model_copy(update={"project_version": 99}),
            request.model_copy(update={"input_hash": "not-current"}),
            request.model_copy(update={"catalogue_hash": "not-current"}),
        ):
            with pytest.raises(StoreError) as error:
                runtime.decisions.submit(
                    run["run_id"],
                    stale,
                    principal=_manager(runtime),
                    idempotency_key=f"stale-{id(stale)}",
                )
            assert error.value.code == "STALE_DECISION"
        assert runtime.store.get_decision(run["run_id"]) is None
    finally:
        runtime.close()


def test_approval_creates_one_revision_and_idempotent_replay(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        run = _awaiting_run(runtime)
        request = _request(run)
        first = runtime.decisions.submit(
            run["run_id"],
            request,
            principal=_manager(runtime),
            idempotency_key="approve-1",
        )
        assert first["revision"]["revision"] == 1
        assert runtime.store.get_run(run["run_id"])["decision_status"] == "approved"

        replay = runtime.decisions.submit(
            run["run_id"],
            request,
            principal=_manager(runtime),
            idempotency_key="approve-1",
        )
        assert replay["idempotent_replay"] is True
        assert replay["revision"]["revision_id"] == first["revision"]["revision_id"]
        assert len(runtime.store.list_revisions(run["run_id"])) == 1
        decision_events = [
            event
            for event in runtime.store.list_audit(run["run_id"])
            if event["event_type"] == "DECISION_APPROVED"
        ]
        assert len(decision_events) == 1

        assert runtime.worker.run_once()["status"] == "completed"
        completed = runtime.store.get_run(run["run_id"])
        assert completed["status"] == "completed"
        assert completed["revision"] == 1
    finally:
        runtime.close()


def test_idempotency_key_reuse_with_different_content_conflicts(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        run = _awaiting_run(runtime)
        request = _request(run)
        runtime.decisions.submit(
            run["run_id"],
            request,
            principal=_manager(runtime),
            idempotency_key="shared-key",
        )
        with pytest.raises(StoreError) as error:
            runtime.decisions.submit(
                run["run_id"],
                request.model_copy(update={"approved": False}),
                principal=_manager(runtime),
                idempotency_key="shared-key",
            )
        assert error.value.code == "IDEMPOTENCY_CONFLICT"
    finally:
        runtime.close()


def test_rejection_does_not_create_a_revision(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        run = _awaiting_run(runtime)
        result = runtime.decisions.submit(
            run["run_id"],
            _request(run).model_copy(update={"approved": False}),
            principal=_manager(runtime),
            idempotency_key="reject-1",
        )
        assert result["revision"] is None
        assert runtime.store.list_revisions(run["run_id"]) == []
        assert runtime.store.get_run(run["run_id"])["status"] == "rejected"
        assert runtime.worker.run_once()["status"] == "rejected"
        assert runtime.store.get_run(run["run_id"])["status"] == "rejected"
    finally:
        runtime.close()


def test_competing_decisions_produce_one_winner_and_one_conflict(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        run = _awaiting_run(runtime)
        request = _request(run)
        manager = _manager(runtime)

        def submit(key: str):
            try:
                return runtime.decisions.submit(
                    run["run_id"],
                    request,
                    principal=manager,
                    idempotency_key=key,
                )
            except StoreError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, ["race-a", "race-b"]))
        successes = [item for item in results if isinstance(item, dict)]
        conflicts = [item for item in results if item == "DECISION_CONFLICT"]
        assert len(successes) == 1
        assert len(conflicts) == 1
        assert len(runtime.store.list_revisions(run["run_id"])) == 1
    finally:
        runtime.close()


def test_commit_before_checkpoint_advancement_recovers_without_duplicate(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        run = _awaiting_run(runtime)
        request = _request(run)
        first = runtime.decisions.submit(
            run["run_id"],
            request,
            principal=_manager(runtime),
            idempotency_key="crash-window",
        )
        assert first["revision"]["revision"] == 1
        # Deliberately do not run the worker. Close the runtime as if the
        # process stopped after the application transaction committed.
    finally:
        runtime.close()

    rebuilt = build_test_runtime(tmp_path)
    try:
        assert rebuilt.worker.run_once()["status"] == "completed"
        run = rebuilt.store.get_run("run-decision")
        assert run["status"] == "completed"
        assert len(rebuilt.store.list_revisions("run-decision")) == 1
        replay = rebuilt.decisions.submit(
            "run-decision",
            request,
            principal=_manager(rebuilt),
            idempotency_key="crash-window",
        )
        assert replay["idempotent_replay"] is True
        assert len(rebuilt.store.list_revisions("run-decision")) == 1
    finally:
        rebuilt.close()


def test_decision_missing_run_candidate_and_infeasible_paths(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        with pytest.raises(StoreError) as missing:
            runtime.decisions.submit(
                "missing-run",
                DecisionRequest(
                    candidate_id="C",
                    project_version=1,
                    input_hash="i",
                    catalogue_hash="c",
                    approved=True,
                ),
                principal=_manager(runtime),
                idempotency_key="missing",
            )
        assert missing.value.code == "RUN_NOT_FOUND"

        run = _awaiting_run(runtime, "run-paths")
        request = _request(run)
        with pytest.raises(StoreError) as unknown_candidate:
            runtime.decisions.submit(
                run["run_id"],
                request.model_copy(update={"candidate_id": "CAND-UNKNOWN"}),
                principal=_manager(runtime),
                idempotency_key="unknown-candidate",
            )
        assert unknown_candidate.value.code == "STALE_DECISION"

        evaluation = runtime.store.get_run(run["run_id"])["evaluation"]
        infeasible = next(
            candidate
            for candidate in evaluation["candidates"]
            if not candidate["feasible"]
        )
        with pytest.raises(StoreError) as not_feasible:
            runtime.decisions.submit(
                run["run_id"],
                DecisionRequest(
                    candidate_id=infeasible["candidate_id"],
                    project_version=run["pending_decision"]["project_version"],
                    input_hash=infeasible["base_hash"],
                    catalogue_hash=infeasible["catalogue_hash"],
                    approved=True,
                ),
                principal=_manager(runtime),
                idempotency_key="infeasible",
            )
        assert not_feasible.value.code == "CANDIDATE_NOT_FEASIBLE"
        assert runtime.decisions._revision_for_decision(
            run["run_id"], {"revision_id": "missing-revision"}
        ) is None
    finally:
        runtime.close()


def test_second_decision_after_commit_conflicts(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        run = _awaiting_run(runtime, "run-second")
        request = _request(run)
        runtime.decisions.submit(
            run["run_id"],
            request,
            principal=_manager(runtime),
            idempotency_key="first-key",
        )
        with pytest.raises(StoreError) as error:
            runtime.decisions.submit(
                run["run_id"],
                request,
                principal=_manager(runtime),
                idempotency_key="second-key",
            )
        assert error.value.code == "DECISION_CONFLICT"
    finally:
        runtime.close()


def _create_versioned_run(runtime, run_id: str, *, value=None):
    value = value or draft()
    project = value.to_project_input()
    evaluation = evaluate_recovery(project, value.disruption)
    base = evaluation.candidates[0]
    runtime.store.create_run(
        draft_json=value.model_dump(mode="json"),
        request_hash=f"hash-{run_id}",
        run_id=run_id,
        thread_id=run_id,
        principal_id="planner.demo",
        idempotency_key=f"create-{run_id}",
        project_state={
            "project_id": project.project_id,
            "version": project.version,
            "input_hash": base.base_hash,
            "catalogue_hash": base.catalogue_hash,
            "project_json": project.model_dump(mode="json"),
        },
    )
    result = runtime.worker.run_once()
    assert result["status"] == "awaiting_approval"
    return runtime.store.get_run(run_id)


def test_second_run_for_same_version_cannot_approve(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        first_run = _create_versioned_run(runtime, "run-version-a")
        second_run = _create_versioned_run(runtime, "run-version-b")
        first = runtime.decisions.submit(
            first_run["run_id"],
            _request(first_run),
            principal=_manager(runtime),
            idempotency_key="version-a",
        )
        assert first["revision"]["revision"] == 1
        with pytest.raises(StoreError) as stale:
            runtime.decisions.submit(
                second_run["run_id"],
                _request(second_run),
                principal=_manager(runtime),
                idempotency_key="version-b",
            )
        assert stale.value.code == "STALE_DECISION"
        current = runtime.store.get_project_version("UTILITY-DEMO-001")
        assert current["version"] == 2
        assert len(runtime.store.list_revisions(first_run["run_id"])) == 1
        assert len(runtime.store.list_revisions(second_run["run_id"])) == 0
        assert runtime.store.get_decision(second_run["run_id"]) is None
    finally:
        runtime.close()


def test_concurrent_runs_for_same_version_produce_one_winner(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        first_run = _create_versioned_run(runtime, "run-race-a")
        second_run = _create_versioned_run(runtime, "run-race-b")
        manager = _manager(runtime)

        def submit(run, key):
            try:
                return runtime.decisions.submit(
                    run["run_id"],
                    _request(run),
                    principal=manager,
                    idempotency_key=key,
                )
            except StoreError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda pair: submit(*pair),
                    [(first_run, "race-version-a"), (second_run, "race-version-b")],
                )
            )
        winners = [item for item in results if isinstance(item, dict)]
        conflicts = [
            item
            for item in results
            if isinstance(item, str) and item in {"STALE_DECISION", "DECISION_CONFLICT"}
        ]
        assert len(winners) == 1
        assert len(conflicts) == 1
        assert len(runtime.store.list_revisions(first_run["run_id"])) + len(
            runtime.store.list_revisions(second_run["run_id"])
        ) == 1
    finally:
        runtime.close()


def test_rejection_does_not_advance_project_version(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        run = _create_versioned_run(runtime, "run-reject-version")
        before = runtime.store.get_project_version("UTILITY-DEMO-001")
        runtime.decisions.submit(
            run["run_id"],
            _request(run).model_copy(update={"approved": False}),
            principal=_manager(runtime),
            idempotency_key="reject-version",
        )
        after = runtime.store.get_project_version("UTILITY-DEMO-001")
        assert before["version"] == after["version"] == 1
        assert runtime.store.list_revisions(run["run_id"]) == []
    finally:
        runtime.close()


def test_run_creation_policy_rejects_roleless_principal():
    from delivery_recovery.api.identity import Principal
    from delivery_recovery.approvals import authorise_run_create

    with pytest.raises(StoreError) as denied:
        authorise_run_create(
            Principal(actor_id="nobody", roles=()), draft()
        )
    assert denied.value.code == "FORBIDDEN"


def test_pending_binding_mismatch_is_stale(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        run = _awaiting_run(runtime, "run-pending-mismatch")
        request = _request(run)
        connection = runtime.store._connect()
        pending = dict(run["pending_decision"])
        pending["candidate_id"] = "CAND-OTHER"
        connection.execute(
            "UPDATE runs SET pending_decision_json = ? WHERE run_id = ?",
            (__import__("json").dumps(pending), run["run_id"]),
        )
        connection.close()
        with pytest.raises(StoreError) as stale:
            runtime.decisions.submit(
                run["run_id"],
                request,
                principal=_manager(runtime),
                idempotency_key="pending-mismatch",
            )
        assert stale.value.code == "STALE_DECISION"
    finally:
        runtime.close()
