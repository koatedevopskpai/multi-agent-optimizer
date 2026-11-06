"""Before/after probe for Phase 3 R1-R3 corrections.

Run with DELIVERY_RECOVERY_SRC pointing at an older source tree to record the
pre-fix behaviour, or without it for the corrected behaviour.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = Path(os.environ.get("DELIVERY_RECOVERY_SRC", REPO / "src"))
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(REPO / "tests"))

from fastapi.testclient import TestClient  # noqa: E402

from delivery_recovery.api.app import create_app  # noqa: E402
from delivery_recovery.api.identity import Principal  # noqa: E402
from delivery_recovery.api.runtime import build_runtime  # noqa: E402
from delivery_recovery.storage.config import StorageConfig  # noqa: E402
from phase3_support import draft, tokens  # noqa: E402


def fresh_runtime(root: Path):
    data = root / "data"
    config = StorageConfig(
        data_dir=data,
        db_path=data / "application.db",
        checkpoint_path=data / "checkpoints.db",
    )
    return build_runtime(config, start_worker=False)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def case_r1(root: Path) -> dict:
    runtime = fresh_runtime(root)
    try:
        runtime.identity.add(
            "outsider",
            Principal(
                actor_id="outsider",
                roles=("planner",),
                project_ids=("OTHER-PROJECT",),
            ),
        )
        client = TestClient(create_app(runtime))
        response = client.post(
            "/v1/runs",
            json={"draft": draft().model_dump(mode="json")},
            headers=auth("outsider"),
        )
        connection = runtime.store._connect()
        runs = connection.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        jobs = connection.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"]
        connection.close()
        return {
            "http_status": response.status_code,
            "runs_created": runs,
            "jobs_created": jobs,
        }
    finally:
        runtime.close()


def case_r2(root: Path) -> dict:
    runtime = fresh_runtime(root)
    try:
        client = TestClient(create_app(runtime))
        planner = tokens(runtime)["planner.demo"]
        manager = tokens(runtime)["manager.demo"]
        created = []
        for index in range(2):
            response = client.post(
                "/v1/runs",
                json={"draft": draft().model_dump(mode="json")},
                headers={**auth(planner), "Idempotency-Key": f"probe-r2-{index}"},
            )
            created.append(response.json()["run_id"])
        for _ in created:
            runtime.worker.run_once()
        outcomes = []
        for index, run_id in enumerate(created):
            run = client.get(f"/v1/runs/{run_id}", headers=auth(planner)).json()
            binding = run["pending_decision"]
            response = client.post(
                f"/v1/runs/{run_id}/decisions",
                json={
                    "candidate_id": binding["candidate_id"],
                    "project_version": binding["project_version"],
                    "input_hash": binding["input_hash"],
                    "catalogue_hash": binding["catalogue_hash"],
                    "approved": True,
                },
                headers={
                    **auth(manager),
                    "Idempotency-Key": f"probe-r2-decision-{index}",
                },
            )
            outcomes.append(
                {
                    "status": response.status_code,
                    "body_code": response.json().get("error", {}).get("code")
                    if response.status_code >= 400
                    else None,
                }
            )
        return {"decision_outcomes": outcomes}
    finally:
        runtime.close()


def case_r3(root: Path) -> dict:
    runtime = fresh_runtime(root)
    try:
        client = TestClient(create_app(runtime))
        planner = tokens(runtime)["planner.demo"]
        manager = tokens(runtime)["manager.demo"]
        value = draft()
        install_only = [
            option for option in value.catalogue if option.option_id == "INSTALL_SUPPORT"
        ]
        value = value.model_copy(
            update={
                "target_finish_offset_workdays": 16,
                "catalogue": install_only,
            }
        )
        created = client.post(
            "/v1/runs",
            json={"draft": value.model_dump(mode="json")},
            headers={**auth(planner), "Idempotency-Key": "probe-r3"},
        )
        run_id = created.json()["run_id"]
        runtime.worker.run_once()
        run = client.get(f"/v1/runs/{run_id}", headers=auth(planner)).json()
        binding = run["pending_decision"]
        decision = client.post(
            f"/v1/runs/{run_id}/decisions",
            json={
                "candidate_id": binding["candidate_id"],
                "project_version": binding["project_version"],
                "input_hash": binding["input_hash"],
                "catalogue_hash": binding["catalogue_hash"],
                "approved": True,
            },
            headers={**auth(manager), "Idempotency-Key": "probe-r3-decision"},
        )
        audit = client.get(f"/v1/runs/{run_id}/audit", headers=auth(manager)).json()
        plan = audit["revisions"][0]["plan"] if audit["revisions"] else None
        durations = (
            {task["id"]: task["duration_workdays"] for task in plan["tasks"]}
            if plan
            else {}
        )
        return {
            "decision_status": decision.status_code,
            "revision_count": len(audit["revisions"]),
            "delayed_task_C": durations.get("C"),
            "installation_task_D": durations.get("D"),
        }
    finally:
        runtime.close()


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="p3-correction-probe-"))
    try:
        results = {
            "src": str(SRC),
            "R1_wrong_project_creation": case_r1(root / "r1"),
            "R2_same_version_two_approvals": case_r2(root / "r2"),
            "R3_install_support_revision": case_r3(root / "r3"),
        }
        print(json.dumps(results, indent=2, sort_keys=True))
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
