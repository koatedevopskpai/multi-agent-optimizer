"""Actual-process restart drill for Phase 3 local storage.

Run ``prepare`` and ``recover`` as separate Python processes against the same
data directory. No provider network call is made.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

from delivery_recovery.api.runtime import build_runtime  # noqa: E402
from delivery_recovery.approvals import DecisionRequest  # noqa: E402
from delivery_recovery.storage.config import StorageConfig  # noqa: E402
from phase3_support import draft  # noqa: E402


def _runtime(data: Path):
    config = StorageConfig(
        data_dir=data,
        db_path=data / "application.db",
        checkpoint_path=data / "checkpoints.db",
        lease_seconds=30,
        pending_retention_seconds=3600,
    )
    return build_runtime(config, start_worker=False, source_revision="restart-drill")


def _state_path(data: Path) -> Path:
    return data / "drill-state.json"


def _manager(runtime):
    return next(
        identity.principal
        for identity in runtime.identity.identities
        if identity.principal.is_manager()
    )


def prepare(data: Path, mode: str) -> dict:
    data.mkdir(parents=True, exist_ok=True)
    runtime = _runtime(data)
    try:
        value = draft()
        run_id = f"drill-{mode}"
        runtime.store.create_run(
            draft_json=value.model_dump(mode="json"),
            request_hash=f"request-{mode}",
            run_id=run_id,
            thread_id=run_id,
            principal_id="planner.demo",
            idempotency_key=f"create-{mode}",
        )
        if mode == "queued":
            status = runtime.store.get_run(run_id)["status"]
        else:
            result = runtime.worker.run_once()
            run = runtime.store.get_run(run_id)
            status = run["status"]
            if mode == "crash-window":
                binding = run["pending_decision"]
                runtime.decisions.submit(
                    run_id,
                    DecisionRequest(
                        candidate_id=binding["candidate_id"],
                        project_version=binding["project_version"],
                        input_hash=binding["input_hash"],
                        catalogue_hash=binding["catalogue_hash"],
                        approved=True,
                    ),
                    principal=_manager(runtime),
                    idempotency_key=f"decision-{mode}",
                )
                status = runtime.store.get_run(run_id)["status"]
        payload = {
            "phase": "prepare",
            "mode": mode,
            "run_id": run_id,
            "thread_id": run_id,
            "status_at_exit": status,
            "worker_result": locals().get("result"),
        }
        _state_path(data).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return payload
    finally:
        runtime.close()


def recover(data: Path, mode: str) -> dict:
    state = json.loads(_state_path(data).read_text(encoding="utf-8"))
    runtime = _runtime(data)
    try:
        run_id = state["run_id"]
        run = runtime.store.get_run(run_id)
        if run is None:
            raise SystemExit(f"run missing: {run_id}")
        if mode == "approval" and run["status"] == "awaiting_approval":
            binding = run["pending_decision"]
            runtime.decisions.submit(
                run_id,
                DecisionRequest(
                    candidate_id=binding["candidate_id"],
                    project_version=binding["project_version"],
                    input_hash=binding["input_hash"],
                    catalogue_hash=binding["catalogue_hash"],
                    approved=True,
                ),
                principal=_manager(runtime),
                idempotency_key=f"decision-{mode}",
            )
        result = runtime.worker.run_once()
        final = runtime.store.get_run(run_id)
        payload = {
            "phase": "recover",
            "mode": mode,
            "run_id": run_id,
            "thread_id": final["thread_id"],
            "status_after_restart": final["status"],
            "revision": final["revision"],
            "decision_status": final["decision_status"],
            "worker_result": result,
            "revisions": [
                {
                    "revision": item["revision"],
                    "candidate_id": item["candidate_id"],
                }
                for item in runtime.store.list_revisions(run_id)
            ],
            "audit_events": [
                event["event_type"] for event in runtime.store.list_audit(run_id)
            ],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return payload
    finally:
        runtime.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["prepare", "recover"])
    parser.add_argument("--mode", choices=["queued", "approval", "crash-window"], required=True)
    parser.add_argument("--data", required=True)
    args = parser.parse_args()
    data = Path(args.data).resolve()
    payload = prepare(data, args.mode) if args.phase == "prepare" else recover(data, args.mode)
    if args.phase == "prepare":
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
