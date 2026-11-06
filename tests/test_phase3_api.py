"""Phase 3C API contract and local journey tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from delivery_recovery.api.app import create_app
from delivery_recovery.api.identity import Principal

from phase3_support import auth, build_test_runtime, draft, tokens


def _create_payload(**overrides):
    return {"draft": draft(**overrides).model_dump(mode="json")}


def _awaiting(client, runtime, planner_token, run_id):
    response = client.get(f"/v1/runs/{run_id}", headers=auth(planner_token))
    assert response.status_code == 200
    return response.json()


def test_phase3_readiness_reports_storage_schema_and_worker(tmp_path):
    runtime = build_test_runtime(tmp_path, start_worker=True)
    try:
        client = TestClient(create_app(runtime))
        response = client.get("/readyz")
        assert response.status_code == 200
        body = response.json()
        assert body["mode"] == "phase3"
        assert body["storage"] == "ok"
        assert body["schema_version"] == 2
        assert body["worker"] == "running"
        assert body["queue_depth"] == 0
        assert body["pending_human"] == 0
    finally:
        runtime.close()


def test_runs_require_authentication_and_project_access(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        runtime.identity.add(
            "outsider-token",
            Principal(
                actor_id="outsider",
                roles=("manager",),
                project_ids=("OTHER-PROJECT",),
            ),
        )
        client = TestClient(create_app(runtime))
        assert client.get("/v1/runs/missing").status_code == 401
        planner_token = tokens(runtime)["planner.demo"]
        created = client.post(
            "/v1/runs", json=_create_payload(), headers=auth(planner_token)
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["run_id"]
        denied = client.get(
            f"/v1/runs/{run_id}", headers=auth("outsider-token")
        )
        assert denied.status_code == 403
    finally:
        runtime.close()


def test_complete_authorised_journey_and_one_revision(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        client = TestClient(create_app(runtime))
        planner_token = tokens(runtime)["planner.demo"]
        manager_token = tokens(runtime)["manager.demo"]
        created = client.post(
            "/v1/runs", json=_create_payload(), headers=auth(planner_token)
        )
        assert created.status_code == 202
        run_id = created.json()["run_id"]
        assert runtime.worker.run_once()["status"] == "awaiting_approval"
        run = _awaiting(client, runtime, planner_token, run_id)
        assert run["evaluation"]["disrupted"]["finish_offset_workdays"] == 17
        assert run["pending_decision"]["finish_offset_workdays"] == 15
        assert run["pending_decision"]["cost_pence"] == 180_000

        body = {
            "candidate_id": run["pending_decision"]["candidate_id"],
            "project_version": run["pending_decision"]["project_version"],
            "input_hash": run["pending_decision"]["input_hash"],
            "catalogue_hash": run["pending_decision"]["catalogue_hash"],
            "approved": True,
        }
        planner_denied = client.post(
            f"/v1/runs/{run_id}/decisions",
            json=body,
            headers={**auth(planner_token), "Idempotency-Key": "decision-api"},
        )
        assert planner_denied.status_code == 403

        approved = client.post(
            f"/v1/runs/{run_id}/decisions",
            json=body,
            headers={**auth(manager_token), "Idempotency-Key": "decision-api"},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["revision"]["revision"] == 1
        replay = client.post(
            f"/v1/runs/{run_id}/decisions",
            json=body,
            headers={**auth(manager_token), "Idempotency-Key": "decision-api"},
        )
        assert replay.status_code == 200
        assert replay.json()["idempotent_replay"] is True
        assert runtime.worker.run_once()["status"] == "completed"
        completed = client.get(f"/v1/runs/{run_id}", headers=auth(planner_token)).json()
        assert completed["status"] == "completed"
        assert completed["revision"] == 1
        audit = client.get(
            f"/v1/runs/{run_id}/audit", headers=auth(manager_token)
        ).json()
        assert len(audit["revisions"]) == 1
        assert (
            sum(1 for event in audit["events"] if event["event_type"] == "DECISION_APPROVED")
            == 1
        )
    finally:
        runtime.close()


def test_stale_decision_returns_conflict(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        client = TestClient(create_app(runtime))
        planner_token = tokens(runtime)["planner.demo"]
        manager_token = tokens(runtime)["manager.demo"]
        created = client.post(
            "/v1/runs", json=_create_payload(), headers=auth(planner_token)
        )
        run_id = created.json()["run_id"]
        runtime.worker.run_once()
        run = _awaiting(client, runtime, planner_token, run_id)
        body = {
            "candidate_id": run["pending_decision"]["candidate_id"],
            "project_version": 99,
            "input_hash": run["pending_decision"]["input_hash"],
            "catalogue_hash": run["pending_decision"]["catalogue_hash"],
            "approved": True,
        }
        response = client.post(
            f"/v1/runs/{run_id}/decisions",
            json=body,
            headers={**auth(manager_token), "Idempotency-Key": "stale-api"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "STALE_DECISION"
    finally:
        runtime.close()


def test_clarification_round_trip_keeps_thread_identity(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        client = TestClient(create_app(runtime))
        planner_token = tokens(runtime)["planner.demo"]
        created = client.post(
            "/v1/runs",
            json=_create_payload(
                target_finish_offset_workdays=None, budget_pence=None
            ),
            headers=auth(planner_token),
        )
        run_id = created.json()["run_id"]
        thread_id = created.json()["thread_id"]
        assert runtime.worker.run_once()["status"] == "needs_input"
        first = client.get(f"/v1/runs/{run_id}", headers=auth(planner_token)).json()
        assert first["pending_question"]["field"] == "target_finish_offset_workdays"
        response = client.post(
            f"/v1/runs/{run_id}/clarifications",
            json={"field": first["pending_question"]["field"], "answer": 15},
            headers=auth(planner_token),
        )
        assert response.status_code == 202
        assert runtime.worker.run_once()["status"] == "needs_input"
        second = client.get(f"/v1/runs/{run_id}", headers=auth(planner_token)).json()
        assert second["thread_id"] == thread_id
        assert second["pending_question"]["field"] == "budget_pence"
    finally:
        runtime.close()


def test_api_exposes_planned_paths_and_queue_limit(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        client = TestClient(create_app(runtime))
        planner_token = tokens(runtime)["planner.demo"]
        paths = client.get("/openapi.json").json()["paths"]
        for path in (
            "/v1/runs",
            "/v1/runs/{run_id}",
            "/v1/runs/{run_id}/clarifications",
            "/v1/runs/{run_id}/decisions",
            "/v1/runs/{run_id}/audit",
        ):
            assert path in paths
        for _ in range(4):
            assert (
                client.post(
                    "/v1/runs", json=_create_payload(), headers=auth(planner_token)
                ).status_code
                == 202
            )
        rejected = client.post(
            "/v1/runs", json=_create_payload(), headers=auth(planner_token)
        )
        assert rejected.status_code == 429
        assert rejected.json()["error"]["code"] == "QUEUE_FULL"
    finally:
        runtime.close()


def test_readyz_is_not_ready_without_worker(tmp_path):
    runtime = build_test_runtime(tmp_path, start_worker=False)
    try:
        client = TestClient(create_app(runtime))
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["worker"] == "unavailable"
    finally:
        runtime.close()


def test_phase3_endpoints_report_503_when_not_configured():
    from delivery_recovery.api.identity import IdentityProvider, Principal

    application = create_app()
    provider = IdentityProvider()
    provider.add("token", Principal(actor_id="p", roles=("planner",)))
    application.state.identity = provider
    client = TestClient(application)
    response = client.post(
        "/v1/runs",
        json=_create_payload(),
        headers=auth("token"),
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "PHASE3_NOT_CONFIGURED"


def test_missing_run_and_decision_idempotency_errors(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        client = TestClient(create_app(runtime))
        planner_token = tokens(runtime)["planner.demo"]
        manager_token = tokens(runtime)["manager.demo"]
        assert client.get(
            "/v1/runs/missing", headers=auth(planner_token)
        ).status_code == 404
        assert client.get(
            "/v1/runs/missing/audit", headers=auth(planner_token)
        ).status_code == 404
        assert client.post(
            "/v1/runs/missing/clarifications",
            json={"field": "budget_pence", "answer": 1},
            headers=auth(planner_token),
        ).status_code == 404

        created = client.post(
            "/v1/runs", json=_create_payload(), headers=auth(planner_token)
        )
        run_id = created.json()["run_id"]
        runtime.worker.run_once()
        run = _awaiting(client, runtime, planner_token, run_id)
        body = {
            "candidate_id": run["pending_decision"]["candidate_id"],
            "project_version": run["pending_decision"]["project_version"],
            "input_hash": run["pending_decision"]["input_hash"],
            "catalogue_hash": run["pending_decision"]["catalogue_hash"],
            "approved": True,
        }
        missing_key = client.post(
            f"/v1/runs/{run_id}/decisions",
            json=body,
            headers=auth(manager_token),
        )
        assert missing_key.status_code == 400
        assert missing_key.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    finally:
        runtime.close()


def test_clarification_conflicts_and_wrong_state(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        client = TestClient(create_app(runtime))
        planner_token = tokens(runtime)["planner.demo"]
        created = client.post(
            "/v1/runs", json=_create_payload(), headers=auth(planner_token)
        )
        run_id = created.json()["run_id"]
        runtime.worker.run_once()
        conflict = client.post(
            f"/v1/runs/{run_id}/clarifications",
            json={"field": "budget_pence", "answer": 1},
            headers=auth(planner_token),
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "CLARIFICATION_CONFLICT"

        missing = client.post(
            "/v1/runs",
            json=_create_payload(target_finish_offset_workdays=None),
            headers=auth(planner_token),
        )
        missing_id = missing.json()["run_id"]
        runtime.worker.run_once()
        stale = client.post(
            f"/v1/runs/{missing_id}/clarifications",
            json={"field": "budget_pence", "answer": 1},
            headers=auth(planner_token),
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "STALE_CLARIFICATION"
    finally:
        runtime.close()


def test_wrong_project_creation_is_denied_without_side_effects(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        runtime.identity.add(
            "other-project-token",
            Principal(
                actor_id="other.planner",
                roles=("planner",),
                project_ids=("OTHER-PROJECT",),
            ),
        )
        client = TestClient(create_app(runtime))
        connection = runtime.store._connect()
        before = {
            table: connection.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            for table in ("runs", "jobs", "audit_events")
        }
        connection.close()
        response = client.post(
            "/v1/runs",
            json=_create_payload(),
            headers=auth("other-project-token"),
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "PROJECT_FORBIDDEN"
        connection = runtime.store._connect()
        after = {
            table: connection.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            for table in ("runs", "jobs", "audit_events")
        }
        connection.close()
        assert after == before
    finally:
        runtime.close()


def test_idempotency_is_scoped_to_principal(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        runtime.identity.add(
            "second-planner",
            Principal(actor_id="planner.two", roles=("planner",)),
        )
        client = TestClient(create_app(runtime))
        first_token = tokens(runtime)["planner.demo"]
        second_token = "second-planner"
        first = client.post(
            "/v1/runs",
            json=_create_payload(),
            headers={**auth(first_token), "Idempotency-Key": "shared-create"},
        )
        second = client.post(
            "/v1/runs",
            json=_create_payload(),
            headers={**auth(second_token), "Idempotency-Key": "shared-create"},
        )
        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["run_id"] != second.json()["run_id"]
        replay = client.post(
            "/v1/runs",
            json=_create_payload(),
            headers={**auth(second_token), "Idempotency-Key": "shared-create"},
        )
        assert replay.json()["run_id"] == second.json()["run_id"]
        assert replay.json()["idempotent_replay"] is True
    finally:
        runtime.close()


def test_incomplete_project_less_draft_requires_wildcard_scope(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        runtime.identity.add(
            "scoped-planner",
            Principal(
                actor_id="scoped.planner",
                roles=("planner",),
                project_ids=("OTHER-PROJECT",),
            ),
        )
        client = TestClient(create_app(runtime))
        payload = {
            "draft": draft(
                project_id="DRAFT-PROJECT",
                target_finish_offset_workdays=None,
                budget_pence=None,
            ).model_dump(mode="json")
        }
        denied = client.post(
            "/v1/runs", json=payload, headers=auth("scoped-planner")
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "PROJECT_FORBIDDEN"
        allowed = client.post(
            "/v1/runs", json=payload, headers=auth(tokens(runtime)["planner.demo"])
        )
        assert allowed.status_code == 202
    finally:
        runtime.close()
