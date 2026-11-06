"""Explicit, bounded DeepSeek + LangSmith live acceptance command.

Default mode is a configuration preflight with no external calls. Add
``--execute-live`` only within established provider/LangSmith access and
spending authority. Secret values are loaded from a dotenv file without shell
execution and are never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from delivery_recovery import Disruption, ProjectInput, RecoveryOption, TaskDurationChange, TaskInput, evaluate_recovery
from delivery_recovery.workflow.contracts import DraftRecoveryRequest
from delivery_recovery.workflow.exporters import LangSmithClientSink, LangSmithTraceExporter
from delivery_recovery.workflow.live import BudgetedProvider, LiveBudget, build_deepseek_provider
from delivery_recovery.workflow.providers import FakeProvider, ProviderResponse
from delivery_recovery.workflow.service import RecoveryWorkflow, WorkflowLimits

DEFAULT_SECRETS = str(Path.home() / ".secrets" / "focused-delivery-recovery.env")
ALLOWED_KEYS = ("DEEPSEEK_API_KEY", "LANGSMITH_API_KEY")
LANGSMITH_AWS_US = "https://aws.api.smith.langchain.com"
PROJECT = "focused-delivery-recovery"
FIXTURES = Path(__file__).parent.parent / "tests" / "fixtures" / "delivery_recovery" / "utility_recovery.json"


def load_secrets(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in ALLOWED_KEYS:
            values[key] = value.strip().strip('"').strip("'")
    return values


def draft() -> DraftRecoveryRequest:
    raw = json.loads(FIXTURES.read_text(encoding="utf-8"))
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


def safe_result(result) -> dict:
    evaluation = result.evaluation
    return {
        "run_id": result.run_id,
        "thread_id": result.thread_id,
        "status": result.status,
        "provider_outcome": result.provider_outcome.model_dump(mode="json")
        if result.provider_outcome
        else None,
        "explanation": result.explanation.model_dump(mode="json")
        if result.explanation
        else None,
        "evaluation": {
            "baseline_finish": evaluation.baseline.finish_offset_workdays,
            "disrupted_finish": evaluation.disrupted.finish_offset_workdays,
            "candidate_count": len(evaluation.candidates),
            "recommended_candidate_id": evaluation.recommended_candidate_id,
        }
        if evaluation
        else None,
        "errors": [error.model_dump(mode="json") for error in result.errors],
        "trace_export": result.trace_export,
        "tool_spans": [
            span["name"]
            for span in (result.trace or {}).get("spans", [])
            if span.get("kind") == "tool"
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--secrets", type=Path, default=Path(os.getenv("FOCUSED_SECRETS_FILE", DEFAULT_SECRETS)))
    parser.add_argument("--source-revision", default="unknown")
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--langsmith-endpoint", default=LANGSMITH_AWS_US)
    parser.add_argument("--project", default=PROJECT)
    parser.add_argument("--max-cost-gbp", type=float, default=5.0)
    parser.add_argument("--max-tool-calls", type=int, default=5)
    parser.add_argument("--max-attempts", type=int, default=10)
    parser.add_argument("--max-input-tokens", type=int, default=30_000)
    parser.add_argument("--max-output-tokens-total", type=int, default=4_096)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    secrets = load_secrets(args.secrets)
    preflight = {
        "mode": "configuration_preflight_only",
        "deepseek_api_key": "present" if secrets.get("DEEPSEEK_API_KEY") else "missing",
        "langsmith_api_key": "present" if secrets.get("LANGSMITH_API_KEY") else "missing",
        "openai_api_key_used": False,
        "model": "deepseek-flash",
        "base_url": "https://api.deepseek.com",
        "thinking": {"type": "disabled"},
        "langsmith_endpoint": args.langsmith_endpoint,
        "project": args.project,
        "source_revision": args.source_revision,
        "network_calls": 0,
    }
    (args.output_dir / "preflight.json").write_text(json.dumps(preflight, indent=2), encoding="utf-8")
    print(json.dumps(preflight, indent=2))
    if not args.execute_live:
        print("dry_run=true; no external calls made")
        return 0
    if not secrets.get("DEEPSEEK_API_KEY") or not secrets.get("LANGSMITH_API_KEY"):
        print("access=missing; no external calls made")
        return 2

    budget = LiveBudget(
        max_cost_gbp=args.max_cost_gbp,
        max_attempts=args.max_attempts,
        max_input_tokens=args.max_input_tokens,
        max_output_tokens_total=args.max_output_tokens_total,
    )
    provider = BudgetedProvider(
        build_deepseek_provider(secrets["DEEPSEEK_API_KEY"], timeout_seconds=30.0),
        budget,
    )
    from langsmith import Client

    client = Client(
        api_url=args.langsmith_endpoint,
        api_key=secrets["LANGSMITH_API_KEY"],
        auto_batch_tracing=False,
        timeout_ms=15_000,
    )
    sink = LangSmithClientSink(client, project_name=args.project)
    exporter = LangSmithTraceExporter(sink=sink, enabled=True)
    limits = WorkflowLimits(
        max_steps=4,
        max_provider_calls=4,
        max_tool_calls=args.max_tool_calls,
        max_repairs=1,
        max_total_tokens=30_000,
        max_output_tokens=1_024,
        max_elapsed_seconds=30.0,
    )
    workflow = RecoveryWorkflow(
        provider,
        limits=limits,
        source_revision=args.source_revision,
        trace_exporter=exporter,
        trace_metadata={"evidence_class": "live_deepseek", "injected_fault": None},
    )
    fixture_hash = hashlib.sha256(FIXTURES.read_bytes()).hexdigest()
    live = workflow.start(draft())
    live_result = safe_result(live)
    live_result["fixture_hash"] = fixture_hash
    live_result["source_revision"] = args.source_revision
    live_result["timestamp_utc"] = datetime.now(timezone.utc).isoformat()
    live_result["budget"] = budget.summary()
    (args.output_dir / "live_success_result.json").write_text(
        json.dumps(live_result, indent=2, sort_keys=True), encoding="utf-8"
    )
    (args.output_dir / "live_success_trace.json").write_text(
        json.dumps(live.trace or {}, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(live_result, indent=2))

    # Controlled local failure trace, exported to the same project and labelled.
    fake = FakeProvider(
        [ProviderResponse(content="not json"), ProviderResponse(content="still not json")]
    )
    fake_workflow = RecoveryWorkflow(
        fake,
        limits=limits,
        source_revision=args.source_revision,
        trace_exporter=exporter,
        trace_metadata={
            "evidence_class": "controlled_fake",
            "injected_fault": "schema_invalid_fake_provider",
        },
    )
    injected = fake_workflow.start(draft())
    injected_result = safe_result(injected)
    injected_result["fixture_hash"] = fixture_hash
    injected_result["source_revision"] = args.source_revision
    injected_result["timestamp_utc"] = datetime.now(timezone.utc).isoformat()
    injected_result["evidence_class"] = "controlled_fake"
    injected_result["injected_fault"] = "schema_invalid_fake_provider"
    (args.output_dir / "injected_fallback_result.json").write_text(
        json.dumps(injected_result, indent=2, sort_keys=True), encoding="utf-8"
    )
    (args.output_dir / "injected_fallback_trace.json").write_text(
        json.dumps(injected.trace or {}, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(injected_result, indent=2))

    try:
        client.flush()
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
