"""Business smoke test for a running container or AWS instance.

Tokens are read from the local identity file and never printed. The check uses
the real HTTP API, the real worker and the persisted SQLite stores.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_PAYLOAD = SCRIPT_DIR / "phase4_smoke_payload.json"


def _identities(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {item["actor_id"]: item["token"] for item in payload["identities"]}


def _wait(client: httpx.Client, token: str, run_id: str, wanted: set[str]) -> dict:
    deadline = time.time() + 30
    run: dict = {}
    while time.time() < deadline:
        response = client.get(
            f"/v1/runs/{run_id}", headers={"Authorization": f"Bearer {token}"}
        )
        response.raise_for_status()
        run = response.json()
        if run["status"] in wanted:
            return run
        time.sleep(0.2)
    raise SystemExit(f"timed out waiting for {wanted}; last status {run.get('status')}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--identity-file", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--source-revision", default="unknown")
    parser.add_argument("--payload", default=str(DEFAULT_PAYLOAD))
    args = parser.parse_args()

    identities = _identities(Path(args.identity_file))
    planner = identities["planner.demo"]
    manager = identities["manager.demo"]
    suffix = datetime.now(timezone.utc).strftime("%H%M%S")
    project_id = f"UTILITY-SMOKE-{suffix}"
    payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
    payload["project_id"] = project_id
    result: dict = {
        "base_url": args.base_url,
        "source_revision": args.source_revision,
        "project_id": project_id,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    with httpx.Client(base_url=args.base_url, timeout=20.0) as client:
        health = client.get("/healthz")
        ready = client.get("/readyz")
        result["health_status"] = health.status_code
        result["ready_status"] = ready.status_code
        result["ready"] = ready.json()
        health.raise_for_status()
        ready.raise_for_status()

        value = payload
        created = client.post(
            "/v1/runs",
            headers={
                "Authorization": f"Bearer {planner}",
                "Idempotency-Key": f"smoke-create-{suffix}",
            },
            json={"draft": value},
        )
        created.raise_for_status()
        run_id = created.json()["run_id"]
        run = _wait(client, planner, run_id, {"awaiting_approval"})
        binding = run["pending_decision"]
        result["run_id"] = run_id
        result["awaiting_status"] = run["status"]
        result["pending_cost_pence"] = binding["cost_pence"]
        result["pending_finish"] = binding["finish_offset_workdays"]

        decision = client.post(
            f"/v1/runs/{run_id}/decisions",
            headers={
                "Authorization": f"Bearer {manager}",
                "Idempotency-Key": f"smoke-approve-{suffix}",
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
        completed = _wait(
            client, planner, run_id, {"completed", "degraded", "failed"}
        )
        audit = client.get(
            f"/v1/runs/{run_id}/audit",
            headers={"Authorization": f"Bearer {manager}"},
        )
        audit.raise_for_status()
        body = audit.json()
        result.update(
            {
                "completed_status": completed["status"],
                "revision": completed["revision"],
                "decision_status": completed["decision_status"],
                "audit_events": [event["event_type"] for event in body["events"]],
                "revision_count": len(body["revisions"]),
                "revision_plan_version": body["revisions"][0]["plan"]["version"],
                "finished_at_utc": datetime.now(timezone.utc).isoformat(),
            }
        )

    if result["completed_status"] != "completed":
        raise SystemExit(f"smoke did not complete: {result['completed_status']}")
    if result["revision"] != 1 or result["revision_count"] != 1:
        raise SystemExit("smoke expected exactly one committed revision")
    if "DECISION_APPROVED" not in result["audit_events"]:
        raise SystemExit("smoke audit is missing DECISION_APPROVED")
    Path(args.out).write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
