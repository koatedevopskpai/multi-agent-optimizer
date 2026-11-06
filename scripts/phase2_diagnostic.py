"""Run the controlled Phase 2 schema-failure diagnostic with a fake provider.

This is not a production incident. It injects invalid model output, records the
local span tree and writes a sanitised trace JSON file. No network or provider
credentials are used.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from delivery_recovery import Disruption, ProjectInput, RecoveryOption, TaskDurationChange, TaskInput
from delivery_recovery.workflow.contracts import DraftRecoveryRequest
from delivery_recovery.workflow.providers import FakeProvider, ProviderResponse
from delivery_recovery.workflow.service import RecoveryWorkflow


def _draft() -> DraftRecoveryRequest:
    raw = json.loads(
        (Path(__file__).parent.parent / "tests" / "fixtures" / "delivery_recovery" / "utility_recovery.json").read_text(
            encoding="utf-8"
        )
    )
    project = ProjectInput(
        project_id=raw["project_id"],
        version=raw["project_version"],
        start_offset_workdays=raw["start_offset_workdays"],
        target_finish_offset_workdays=raw["target_finish_offset_workdays"],
        budget_pence=raw["budget_pence"],
        tasks=[
            TaskInput(
                id=task_id,
                name=body["name"],
                duration_workdays=body["duration"],
                dependency_ids=body["dependencies"],
            )
            for task_id, body in raw["baseline_tasks"].items()
        ],
        catalogue=[
            RecoveryOption(
                option_id=option["option_id"],
                changes=[
                    TaskDurationChange(
                        task_id=change["task_id"],
                        new_duration_workdays=change["new_duration_workdays"],
                    )
                    for change in option["changes"]
                ],
                incremental_cost_pence=option["incremental_cost_pence"],
            )
            for option in raw["recovery_catalogue"]
        ],
    )
    disruption = Disruption(
        task_id=raw["disruption"]["task_id"],
        revised_duration_workdays=raw["disruption"]["revised_duration_workdays"],
    )
    return DraftRecoveryRequest(
        project_id=project.project_id,
        version=project.version,
        time_unit=project.time_unit,
        start_offset_workdays=project.start_offset_workdays,
        target_finish_offset_workdays=project.target_finish_offset_workdays,
        budget_pence=project.budget_pence,
        tasks=project.tasks,
        catalogue=project.catalogue,
        disruption=disruption,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-revision", default="unknown")
    args = parser.parse_args()

    provider = FakeProvider(
        [ProviderResponse(content="not json"), ProviderResponse(content="still not json")]
    )
    workflow = RecoveryWorkflow(
        provider,
        source_revision=args.source_revision,
        fixture_hash="utility-recovery-v1",
        prompt_version="p2-v1",
        schema_version="explanation-v1",
    )
    result = workflow.start(_draft())

    trace = result.trace or {}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(trace, indent=2, sort_keys=True), encoding="utf-8")

    print("status", result.status)
    print("provider_calls", len(provider.calls))
    print("reason", result.provider_outcome.reason_code if result.provider_outcome else None)
    print("spans", [span["name"] for span in trace.get("spans", [])])
    print("trace", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
