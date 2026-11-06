"""Contract tests for the deterministic recovery HTTP API.

These tests start before the FastAPI app exists. The API must return the same
domain facts as a direct call; it must not calculate or reinterpret them.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from delivery_recovery.api.app import app

FIXTURES = Path(__file__).parent / "fixtures" / "delivery_recovery"
client = TestClient(app)


def _utility_payload(*, budget_pence: int = 200_000, incompatible: bool = False) -> dict:
    raw = json.loads((FIXTURES / "utility_recovery.json").read_text(encoding="utf-8"))
    catalogue = []
    for option in raw["recovery_catalogue"]:
        payload = {
            "option_id": option["option_id"],
            "changes": option["changes"],
            "incremental_cost_pence": option["incremental_cost_pence"],
        }
        if incompatible:
            payload["incompatible_option_ids"] = [
                other["option_id"]
                for other in raw["recovery_catalogue"]
                if other["option_id"] != option["option_id"]
            ]
        catalogue.append(payload)
    return {
        "project": {
            "schema_version": "delivery-recovery-v1",
            "project_id": raw["project_id"],
            "version": raw["project_version"],
            "time_unit": raw["time_unit"],
            "start_offset_workdays": raw["start_offset_workdays"],
            "target_finish_offset_workdays": raw["target_finish_offset_workdays"],
            "budget_pence": budget_pence,
            "currency": raw["currency"],
            "tasks": [
                {
                    "id": task_id,
                    "name": body["name"],
                    "duration_workdays": body["duration"],
                    "dependency_ids": body["dependencies"],
                }
                for task_id, body in raw["baseline_tasks"].items()
            ],
            "catalogue": catalogue,
        },
        "disruption": raw["disruption"],
        "option_ids": None,
    }


def _parallel_payload() -> dict:
    project = {
        "project_id": "PARALLEL-DEMO-001",
        "version": 1,
        "start_offset_workdays": 0,
        "target_finish_offset_workdays": 5,
        "budget_pence": 10_000,
        "tasks": [
            {"id": "S", "name": "Start", "duration_workdays": 1},
            {"id": "P", "name": "Branch P", "duration_workdays": 3, "dependency_ids": ["S"]},
            {"id": "Q", "name": "Branch Q", "duration_workdays": 3, "dependency_ids": ["S"]},
            {"id": "E", "name": "End", "duration_workdays": 1, "dependency_ids": ["P", "Q"]},
        ],
        "catalogue": [
            {
                "option_id": "SHORTEN_P",
                "changes": [{"task_id": "P", "new_duration_workdays": 2}],
                "incremental_cost_pence": 500,
            },
            {
                "option_id": "SHORTEN_Q",
                "changes": [{"task_id": "Q", "new_duration_workdays": 2}],
                "incremental_cost_pence": 500,
            },
        ],
    }
    return {"project": project, "disruption": None, "option_ids": None}


def test_health_endpoint():
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_utility_evaluation_returns_domain_facts():
    response = client.post("/v1/recovery/evaluations", json=_utility_payload())
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["baseline"]["finish_offset_workdays"] == 15
    assert body["disrupted"]["finish_offset_workdays"] == 17
    candidates = {tuple(candidate["option_ids"]): candidate for candidate in body["candidates"]}
    expedite = candidates[("EXPEDITE_PANEL",)]
    assert expedite["finish_offset_workdays"] == 15
    assert expedite["cost_pence"] == 180_000
    assert expedite["project_saving_workdays"] == 2
    assert expedite["feasible"] is True
    assert body["recommended_candidate_id"] == expedite["candidate_id"]


def test_parallel_evaluation_preserves_task_versus_project_saving():
    response = client.post("/v1/recovery/evaluations", json=_parallel_payload())
    assert response.status_code == 200, response.text
    body = response.json()
    candidates = {tuple(candidate["option_ids"]): candidate for candidate in body["candidates"]}

    assert candidates[("SHORTEN_P",)]["task_reductions"] == {"P": 1}
    assert candidates[("SHORTEN_P",)]["project_saving_workdays"] == 0
    assert candidates[("SHORTEN_P", "SHORTEN_Q")]["project_saving_workdays"] == 1


def test_explicit_incompatible_pair_returns_policy_error():
    payload = _utility_payload(incompatible=True)
    payload["option_ids"] = ["EXPEDITE_PANEL", "INSTALL_SUPPORT"]

    response = client.post("/v1/recovery/evaluations", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INCOMPATIBLE_OPTIONS"


def test_automatic_enumeration_excludes_the_incompatible_pair():
    response = client.post(
        "/v1/recovery/evaluations",
        json=_utility_payload(budget_pence=400_000, incompatible=True),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    selections = {tuple(candidate["option_ids"]) for candidate in body["candidates"]}

    assert ("EXPEDITE_PANEL", "INSTALL_SUPPORT") not in selections
    assert ("EXPEDITE_PANEL",) in selections
    assert ("INSTALL_SUPPORT",) in selections


def test_duplicate_task_ids_are_rejected_before_evaluation():
    payload = _utility_payload()
    payload["project"]["tasks"].append(payload["project"]["tasks"][0])

    response = client.post("/v1/recovery/evaluations", json=payload)

    assert response.status_code == 422
    assert "detail" in response.json()


def test_bool_duration_is_rejected_before_evaluation():
    payload = _utility_payload()
    payload["project"]["tasks"][0]["duration_workdays"] = True

    response = client.post("/v1/recovery/evaluations", json=payload)

    assert response.status_code == 422
    assert "detail" in response.json()


def test_ready_endpoint_reports_deterministic_mode():
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "mode": "deterministic"}


def test_root_serves_the_built_ui_or_a_clear_placeholder():
    response = client.get("/", follow_redirects=False)
    assert response.status_code in {200, 307, 308}
    if response.status_code in {307, 308}:
        assert response.headers["location"] == "/ui/"
    else:
        assert "Delivery Recovery Assistant" in response.text


def test_dependency_cycle_returns_structured_invalid_project_error():
    payload = _utility_payload()
    payload["project"]["tasks"][0]["dependency_ids"] = ["G"]

    response = client.post("/v1/recovery/evaluations", json=payload)

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "INVALID_PROJECT"
    assert "cycle" in body["error"]["message"].lower()
    assert "candidates" not in body


def test_duplicate_top_level_json_key_is_rejected_before_validation():
    raw = json.dumps(_utility_payload())
    raw = raw.replace(
        '"budget_pence": 200000',
        '"budget_pence": 200000, "budget_pence": 1',
        1,
    )

    response = client.post(
        "/v1/recovery/evaluations",
        content=raw,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "DUPLICATE_JSON_KEY"
    assert "budget_pence" in body["error"]["message"]
    assert "candidates" not in body


def test_duplicate_nested_json_key_is_rejected_before_validation():
    raw = json.dumps(_utility_payload())
    raw = raw.replace(
        '"duration_workdays": 2',
        '"duration_workdays": 2, "duration_workdays": 9',
        1,
    )

    response = client.post(
        "/v1/recovery/evaluations",
        content=raw,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "DUPLICATE_JSON_KEY"
    assert "duration_workdays" in body["error"]["message"]


def test_oversized_json_body_is_rejected():
    raw = json.dumps(_utility_payload()) + (" " * 70_000)

    response = client.post(
        "/v1/recovery/evaluations",
        content=raw,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"


def test_empty_json_body_is_rejected_by_typed_validation():
    response = client.post(
        "/v1/recovery/evaluations",
        content=b"",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422


def test_malformed_json_is_rejected_by_typed_validation():
    response = client.post(
        "/v1/recovery/evaluations",
        content=b'{"project": ',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
