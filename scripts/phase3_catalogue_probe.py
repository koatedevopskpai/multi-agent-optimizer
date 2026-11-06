"""Before/after probe for the Phase 3 catalogue-binding correction.

Run with DELIVERY_RECOVERY_SRC pointing at the pre-fix source tree to record the
old acceptance of a different catalogue under the same project version, or
without it for the corrected rejection.
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
from delivery_recovery.api.runtime import build_runtime  # noqa: E402
from delivery_recovery.storage.config import StorageConfig  # noqa: E402
from phase3_support import auth, draft, tokens  # noqa: E402


def fresh_runtime(root: Path):
    data = root / "data"
    config = StorageConfig(
        data_dir=data,
        db_path=data / "application.db",
        checkpoint_path=data / "checkpoints.db",
    )
    return build_runtime(config, start_worker=False)


def counts(runtime) -> dict[str, int]:
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


def cheap_expedite():
    value = draft()
    catalogue = [
        option.model_copy(update={"incremental_cost_pence": 100})
        if option.option_id == "EXPEDITE_PANEL"
        else option
        for option in value.catalogue
    ]
    return value.model_copy(update={"catalogue": catalogue})


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="p3-catalogue-probe-"))
    runtime = fresh_runtime(root)
    try:
        client = TestClient(create_app(runtime))
        planner = tokens(runtime)["planner.demo"]
        manager = tokens(runtime)["manager.demo"]

        first = client.post(
            "/v1/runs",
            json={"draft": draft().model_dump(mode="json")},
            headers={**auth(planner), "Idempotency-Key": "probe-cat-first"},
        )
        first.raise_for_status()
        standard = runtime.store.get_project_version("UTILITY-DEMO-001")
        before_second = counts(runtime)

        second = client.post(
            "/v1/runs",
            json={"draft": cheap_expedite().model_dump(mode="json")},
            headers={**auth(planner), "Idempotency-Key": "probe-cat-second"},
        )
        after_second = counts(runtime)
        result = {
            "src": str(SRC),
            "standard_catalogue_hash": standard["catalogue_hash"]
            if standard
            else None,
            "second_create_status": second.status_code,
            "second_create_code": second.json().get("error", {}).get("code")
            if second.status_code >= 400
            else None,
            "runs_after_first_create": before_second["runs"],
            "jobs_after_first_create": before_second["jobs"],
            "runs_after_second_create": after_second["runs"],
            "jobs_after_second_create": after_second["jobs"],
        }

        if second.status_code == 202:
            runtime.worker.run_once()
            runtime.worker.run_once()
            run_id = second.json()["run_id"]
            run = client.get(
                f"/v1/runs/{run_id}", headers=auth(planner)
            ).json()
            binding = run["pending_decision"]
            result["second_run_id"] = run_id
            result["pending_catalogue_hash"] = binding["catalogue_hash"]
            result["pending_cost_pence"] = binding["cost_pence"]
            approval = client.post(
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
                    "Idempotency-Key": "probe-cat-approval",
                },
            )
            result["approval_status"] = approval.status_code
            if approval.status_code == 200:
                revision = approval.json()["revision"]
                result["approved_catalogue_price_pence"] = revision["plan"][
                    "catalogue"
                ][0]["incremental_cost_pence"]
                result["approved_revision"] = revision["revision"]
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    finally:
        runtime.close()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
