"""Seed local CP-04/CP-05 runs through the real HTTP API.

Tokens are read from the server identity file and never printed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from phase3_support import draft  # noqa: E402


def _identities(data_dir: Path) -> dict[str, str]:
    payload = json.loads((data_dir / "demo-identities.json").read_text(encoding="utf-8"))
    return {item["actor_id"]: item["token"] for item in payload["identities"]}


def _post_run(
    client: httpx.Client, token: str, key: str, project_id: str
) -> dict:
    value = draft().model_copy(update={"project_id": project_id})
    response = client.post(
        "/v1/runs",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
        json={"draft": value.model_dump(mode="json")},
    )
    response.raise_for_status()
    return response.json()


def _wait(client: httpx.Client, token: str, run_id: str, wanted: set[str]) -> dict:
    deadline = time.time() + 20
    while time.time() < deadline:
        response = client.get(
            f"/v1/runs/{run_id}", headers={"Authorization": f"Bearer {token}"}
        )
        response.raise_for_status()
        run = response.json()
        if run["status"] in wanted:
            return run
        time.sleep(0.2)
    raise SystemExit(f"timed out waiting for {wanted}: {run['status']}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument(
        "--data-dir",
        default=str(REPO / "var" / "delivery-recovery"),
    )
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    identities = _identities(Path(args.data_dir))
    planner = identities["planner.demo"]
    manager = identities["manager.demo"]
    results: dict = {"base_url": args.base_url, "runs": {}}
    with httpx.Client(base_url=args.base_url, timeout=15.0) as client:
        awaiting = _post_run(client, planner, "cp04-awaiting", "UTILITY-DEMO-101")
        run = _wait(client, planner, awaiting["run_id"], {"awaiting_approval"})
        results["runs"]["cp04_awaiting"] = {
            "run_id": run["run_id"],
            "thread_id": run["thread_id"],
            "status": run["status"],
            "pending_decision": run["pending_decision"],
        }

        completed_create = _post_run(client, planner, "cp05-completed", "UTILITY-DEMO-102")
        completed = _wait(
            client, planner, completed_create["run_id"], {"awaiting_approval"}
        )
        binding = completed["pending_decision"]
        decision = client.post(
            f"/v1/runs/{completed['run_id']}/decisions",
            headers={
                "Authorization": f"Bearer {manager}",
                "Idempotency-Key": "cp05-approval",
            },
            json={
                "candidate_id": binding["candidate_id"],
                "project_version": binding["project_version"],
                "input_hash": binding["input_hash"],
                "catalogue_hash": binding["catalogue_hash"],
                "approved": True,
            },
        )
        decision.raise_for_status()
        final = _wait(
            client, planner, completed["run_id"], {"completed", "degraded"}
        )
        audit = client.get(
            f"/v1/runs/{completed['run_id']}/audit",
            headers={"Authorization": f"Bearer {manager}"},
        ).json()
        results["runs"]["cp05_completed"] = {
            "run_id": final["run_id"],
            "thread_id": final["thread_id"],
            "status": final["status"],
            "revision": final["revision"],
            "decision_status": final["decision_status"],
            "revisions": audit["revisions"],
            "audit_events": [event["event_type"] for event in audit["events"]],
        }

        stale_create = _post_run(client, planner, "cp05-stale", "UTILITY-DEMO-102")
        stale = _wait(client, planner, stale_create["run_id"], {"awaiting_approval"})
        stale_binding = stale["pending_decision"]
        stale_response = client.post(
            f"/v1/runs/{stale['run_id']}/decisions",
            headers={
                "Authorization": f"Bearer {manager}",
                "Idempotency-Key": "cp05-stale",
            },
            json={
                "candidate_id": stale_binding["candidate_id"],
                "project_version": 99,
                "input_hash": stale_binding["input_hash"],
                "catalogue_hash": stale_binding["catalogue_hash"],
                "approved": True,
            },
        )
        results["runs"]["cp05_stale"] = {
            "run_id": stale["run_id"],
            "status": stale["status"],
            "attempt_status_code": stale_response.status_code,
            "attempt_error": stale_response.json().get("error"),
        }

        recovered_create = _post_run(client, planner, "cp05-recovered", "UTILITY-DEMO-103")
        recovered = _wait(
            client, planner, recovered_create["run_id"], {"awaiting_approval"}
        )
        results["runs"]["cp05_recovered_pending"] = {
            "run_id": recovered["run_id"],
            "thread_id": recovered["thread_id"],
            "status_before_restart": recovered["status"],
            "pending_decision": recovered["pending_decision"],
        }
    Path(args.out).write_text(
        json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"seeded": sorted(results["runs"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
