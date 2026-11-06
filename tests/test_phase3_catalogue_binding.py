"""Phase 3 R2 catalogue binding: authoritative version/input/catalogue tuple."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from delivery_recovery.api.app import create_app
from delivery_recovery.approvals import DecisionRequest
from delivery_recovery.candidates import evaluate_recovery
from delivery_recovery.storage.store import StoreError

from phase3_support import auth, build_test_runtime, draft, tokens


def _catalogue_variant(kind: str):
    value = draft()
    if kind == "price":
        catalogue = [
            option.model_copy(update={"incremental_cost_pence": 100})
            if option.option_id == "EXPEDITE_PANEL"
            else option
            for option in value.catalogue
        ]
    elif kind == "duration":
        catalogue = [
            option.model_copy(
                update={
                    "changes": [
                        change.model_copy(update={"new_duration_workdays": 4})
                        if change.task_id == "C"
                        else change
                        for change in option.changes
                    ]
                }
            )
            if option.option_id == "EXPEDITE_PANEL"
            else option
            for option in value.catalogue
        ]
    elif kind == "compatibility":
        catalogue = [
            option.model_copy(
                update={"incompatible_option_ids": ["INSTALL_SUPPORT"]}
            )
            if option.option_id == "EXPEDITE_PANEL"
            else option
            for option in value.catalogue
        ]
    else:
        raise AssertionError(kind)
    return value.model_copy(update={"catalogue": catalogue})


def _create(client, token: str, value, key: str):
    return client.post(
        "/v1/runs",
        json={"draft": value.model_dump(mode="json")},
        headers={**auth(token), "Idempotency-Key": key},
    )


def _counts(runtime) -> dict[str, int]:
    connection = runtime.store._connect()
    try:
        return {
            table: connection.execute(
                f"SELECT COUNT(*) AS c FROM {table}"
            ).fetchone()["c"]
            for table in ("runs", "jobs", "audit_events")
        }
    finally:
        connection.close()


def _awaiting(client, runtime, token: str, run_id: str) -> dict:
    assert runtime.worker.run_once()["status"] == "awaiting_approval"
    response = client.get(f"/v1/runs/{run_id}", headers=auth(token))
    assert response.status_code == 200
    return response.json()


def _binding(run: dict) -> dict:
    pending = run["pending_decision"]
    return {
        "candidate_id": pending["candidate_id"],
        "project_version": pending["project_version"],
        "input_hash": pending["input_hash"],
        "catalogue_hash": pending["catalogue_hash"],
        "approved": True,
    }


def _manager(runtime):
    return next(
        identity.principal
        for identity in runtime.identity.identities
        if identity.principal.is_manager()
    )


@pytest.mark.parametrize("kind", ["price", "duration", "compatibility"])
def test_same_version_catalogue_change_is_rejected_before_persistence(
    tmp_path, kind
):
    runtime = build_test_runtime(tmp_path)
    try:
        client = TestClient(create_app(runtime))
        planner = tokens(runtime)["planner.demo"]
        first = _create(client, planner, draft(), f"catalogue-{kind}-first")
        assert first.status_code == 202, first.text
        before = _counts(runtime)
        authoritative = runtime.store.get_project_version("UTILITY-DEMO-001")
        assert authoritative is not None

        changed = _create(
            client,
            planner,
            _catalogue_variant(kind),
            f"catalogue-{kind}-changed",
        )

        assert changed.status_code == 409, changed.text
        assert changed.json()["error"]["code"] == "PROJECT_VERSION_CONFLICT"
        assert _counts(runtime) == before
        current = runtime.store.get_project_version("UTILITY-DEMO-001")
        assert current["version"] == authoritative["version"] == 1
        assert current["input_hash"] == authoritative["input_hash"]
        assert current["catalogue_hash"] == authoritative["catalogue_hash"]
    finally:
        runtime.close()


def test_unchanged_catalogue_continues_to_create_runs(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        client = TestClient(create_app(runtime))
        planner = tokens(runtime)["planner.demo"]
        first = _create(client, planner, draft(), "unchanged-first")
        second = _create(client, planner, draft(), "unchanged-second")
        assert first.status_code == 202, first.text
        assert second.status_code == 202, second.text
        assert first.json()["run_id"] != second.json()["run_id"]
        assert _counts(runtime)["runs"] == 2
        assert _counts(runtime)["jobs"] == 2
        current = runtime.store.get_project_version("UTILITY-DEMO-001")
        assert current["version"] == 1
    finally:
        runtime.close()


def test_pending_decision_is_stale_when_catalogue_is_no_longer_current(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        client = TestClient(create_app(runtime))
        planner = tokens(runtime)["planner.demo"]
        manager = tokens(runtime)["manager.demo"]
        created = _create(client, planner, draft(), "pending-catalogue-stale")
        run = _awaiting(client, runtime, planner, created.json()["run_id"])
        before = _counts(runtime)

        variant = _catalogue_variant("price")
        project = variant.to_project_input()
        evaluation = evaluate_recovery(project, variant.disruption)
        base = evaluation.candidates[0]
        connection = runtime.store._connect()
        try:
            connection.execute(
                """
                UPDATE project_versions SET catalogue_hash = ?, project_json = ?,
                    updated_at = '2026-11-03T00:00:00+00:00'
                WHERE project_id = ?
                """,
                (
                    base.catalogue_hash,
                    __import__("json").dumps(project.model_dump(mode="json")),
                    project.project_id,
                ),
            )
            connection.commit()
        finally:
            connection.close()

        response = client.post(
            f"/v1/runs/{run['run_id']}/decisions",
            json=_binding(run),
            headers={**auth(manager), "Idempotency-Key": "catalogue-stale-decision"},
        )

        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "STALE_DECISION"
        assert runtime.store.get_decision(run["run_id"]) is None
        assert runtime.store.list_revisions(run["run_id"]) == []
        after = _counts(runtime)
        assert after["audit_events"] == before["audit_events"]
    finally:
        runtime.close()


def test_explicit_new_project_version_can_change_catalogue(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        client = TestClient(create_app(runtime))
        planner = tokens(runtime)["planner.demo"]
        manager = tokens(runtime)["manager.demo"]

        first = _create(client, planner, draft(), "versioned-first")
        old_run = _awaiting(client, runtime, planner, first.json()["run_id"])
        version_one = runtime.store.get_project_version("UTILITY-DEMO-001")

        changed = _catalogue_variant("price")
        changed = changed.model_copy(update={"version": 2})
        second = _create(client, planner, changed, "versioned-second")
        assert second.status_code == 202, second.text
        new_run = _awaiting(client, runtime, planner, second.json()["run_id"])

        project = changed.to_project_input()
        base = evaluate_recovery(project, changed.disruption).candidates[0]
        current = runtime.store.get_project_version("UTILITY-DEMO-001")
        assert current["version"] == 2
        assert current["catalogue_hash"] == base.catalogue_hash
        assert current["catalogue_hash"] != version_one["catalogue_hash"]

        stale = client.post(
            f"/v1/runs/{old_run['run_id']}/decisions",
            json=_binding(old_run),
            headers={**auth(manager), "Idempotency-Key": "versioned-old-decision"},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "STALE_DECISION"

        approved = client.post(
            f"/v1/runs/{new_run['run_id']}/decisions",
            json=_binding(new_run),
            headers={**auth(manager), "Idempotency-Key": "versioned-new-decision"},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["revision"]["project_version"] == 3
    finally:
        runtime.close()


def test_concurrent_conflicting_catalogue_decisions_have_one_winner(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        client = TestClient(create_app(runtime))
        planner = tokens(runtime)["planner.demo"]
        first = _create(client, planner, draft(), "conflict-first")
        old_run = _awaiting(client, runtime, planner, first.json()["run_id"])

        changed = _catalogue_variant("price").model_copy(update={"version": 2})
        second = _create(client, planner, changed, "conflict-second")
        new_run = _awaiting(client, runtime, planner, second.json()["run_id"])

        manager = _manager(runtime)
        requests = {
            old_run["run_id"]: DecisionRequest(**_binding(old_run)),
            new_run["run_id"]: DecisionRequest(**_binding(new_run)),
        }

        def submit(pair):
            run_id, request = pair
            try:
                return run_id, runtime.decisions.submit(
                    run_id,
                    request,
                    principal=manager,
                    idempotency_key=f"conflict-{run_id}",
                )
            except StoreError as error:
                return run_id, error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = dict(pool.map(submit, requests.items()))

        assert isinstance(results[new_run["run_id"]], dict)
        assert results[old_run["run_id"]] == "STALE_DECISION"
        assert len(runtime.store.list_revisions(old_run["run_id"])) == 0
        assert len(runtime.store.list_revisions(new_run["run_id"])) == 1
    finally:
        runtime.close()


def test_older_version_run_is_accepted_only_as_a_stale_candidate(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        client = TestClient(create_app(runtime))
        planner = tokens(runtime)["planner.demo"]
        manager = tokens(runtime)["manager.demo"]

        first = _create(client, planner, draft(), "older-first")
        run = _awaiting(client, runtime, planner, first.json()["run_id"])
        approved = client.post(
            f"/v1/runs/{run['run_id']}/decisions",
            json=_binding(run),
            headers={**auth(manager), "Idempotency-Key": "older-approve"},
        )
        assert approved.status_code == 200, approved.text
        assert runtime.store.get_project_version("UTILITY-DEMO-001")["version"] == 2
        assert runtime.worker.run_once()["status"] == "completed"

        older = _create(client, planner, draft(), "older-again")
        assert older.status_code == 202, older.text
        older_run = _awaiting(client, runtime, planner, older.json()["run_id"])
        stale = client.post(
            f"/v1/runs/{older_run['run_id']}/decisions",
            json=_binding(older_run),
            headers={**auth(manager), "Idempotency-Key": "older-decision"},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "STALE_DECISION"
    finally:
        runtime.close()


def test_skipped_project_version_is_rejected(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        client = TestClient(create_app(runtime))
        planner = tokens(runtime)["planner.demo"]
        first = _create(client, planner, draft(), "skipped-first")
        assert first.status_code == 202, first.text
        before = _counts(runtime)

        skipped = _catalogue_variant("price").model_copy(update={"version": 3})
        response = _create(client, planner, skipped, "skipped-third")

        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "PROJECT_VERSION_CONFLICT"
        assert _counts(runtime) == before
        assert runtime.store.get_project_version("UTILITY-DEMO-001")["version"] == 1
    finally:
        runtime.close()


def test_committed_catalogue_decision_replay_has_no_new_effects(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        client = TestClient(create_app(runtime))
        planner = tokens(runtime)["planner.demo"]
        manager = tokens(runtime)["manager.demo"]
        created = _create(client, planner, draft(), "replay-catalogue")
        run = _awaiting(client, runtime, planner, created.json()["run_id"])
        body = _binding(run)
        headers = {
            **auth(manager),
            "Idempotency-Key": "replay-catalogue-decision",
        }

        first = client.post(
            f"/v1/runs/{run['run_id']}/decisions", json=body, headers=headers
        )
        assert first.status_code == 200, first.text
        revisions = len(runtime.store.list_revisions(run["run_id"]))
        audit = len(runtime.store.list_audit(run["run_id"]))

        replay = client.post(
            f"/v1/runs/{run['run_id']}/decisions", json=body, headers=headers
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert len(runtime.store.list_revisions(run["run_id"])) == revisions == 1
        assert len(runtime.store.list_audit(run["run_id"])) == audit
    finally:
        runtime.close()
