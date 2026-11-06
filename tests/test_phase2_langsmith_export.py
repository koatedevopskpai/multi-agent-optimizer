"""Offline tests for the disabled-by-default LangSmith export seam."""

from __future__ import annotations

from dataclasses import dataclass, field

from delivery_recovery.workflow.exporters import LangSmithTraceExporter
from delivery_recovery.workflow.tracing import TraceRecorder


@dataclass
class RecordingSink:
    started: list[dict] = field(default_factory=list)
    ended: list[dict] = field(default_factory=list)
    fail: bool = False

    def start_run(self, **kwargs) -> str:
        if self.fail:
            raise RuntimeError("sink unavailable")
        self.started.append(kwargs)
        return f"run-{len(self.started)}"

    def end_run(self, **kwargs) -> None:
        if self.fail:
            raise RuntimeError("sink unavailable")
        self.ended.append(kwargs)


def _trace() -> dict:
    tracer = TraceRecorder(
        "run-1",
        metadata={
            "source_revision": "rev-1",
            "fixture_hash": "fixture-1",
            "prompt_version": "p2-v1",
            "schema_version": "explanation-v1",
            "fake_provider": True,
        },
    )
    with tracer.span("assistant", "node"):
        with tracer.span("provider_call:1", "provider", api_key="secret", tokens=12):
            pass
    return tracer.export()


def test_exporter_is_disabled_by_default_and_makes_no_calls():
    sink = RecordingSink()
    exporter = LangSmithTraceExporter(sink=sink)

    result = exporter.export(_trace())

    assert result["exported"] is False
    assert result["reason"] == "disabled"
    assert sink.started == []


def test_enabled_exporter_preserves_parent_child_and_metadata():
    sink = RecordingSink()
    exporter = LangSmithTraceExporter(sink=sink, enabled=True)

    result = exporter.export(_trace())

    assert result["exported"] is True
    assert len(sink.started) == 2
    parent = sink.started[0]
    child = sink.started[1]
    assert parent["metadata"]["source_revision"] == "rev-1"
    assert parent["metadata"]["fake_provider"] is True
    assert child["parent_run_id"] == "run-1"
    assert child["attributes"]["tokens"] == 12
    assert "api_key" not in child["attributes"]
    assert sink.ended != []


def test_export_failure_is_reported_without_raising():
    sink = RecordingSink(fail=True)
    exporter = LangSmithTraceExporter(sink=sink, enabled=True)

    result = exporter.export(_trace())

    assert result["exported"] is False
    assert result["reason"] == "sink_error"
    assert result["error_type"] == "RuntimeError"


def test_enabled_exporter_without_sink_reports_no_sink():
    exporter = LangSmithTraceExporter(enabled=True)

    result = exporter.export(_trace())

    assert result["exported"] is False
    assert result["reason"] == "no_sink"


def test_sanitisation_recurses_through_lists():
    sink = RecordingSink()
    exporter = LangSmithTraceExporter(sink=sink, enabled=True)
    trace = _trace()
    trace["spans"][0]["attributes"] = {
        "nested": [{"api_key": "secret", "tokens": 1}],
    }

    exporter.export(trace)

    attributes = sink.started[0]["attributes"]
    assert attributes["nested"][0]["tokens"] == 1
    assert "api_key" not in attributes["nested"][0]

import json
from pathlib import Path
from datetime import datetime, timezone

from delivery_recovery import Disruption, ProjectInput, RecoveryOption, TaskDurationChange, TaskInput, evaluate_recovery
from delivery_recovery.workflow.contracts import DraftRecoveryRequest
from delivery_recovery.workflow.exporters import LangSmithClientSink
from delivery_recovery.workflow.providers import FakeProvider, ProviderResponse
from delivery_recovery.workflow.service import RecoveryWorkflow

FIXTURES = Path(__file__).parent / "fixtures" / "delivery_recovery"


class MockLangSmithClient:
    def __init__(self):
        self.created = []
        self.updated = []

    def create_run(self, **kwargs):
        self.created.append(kwargs)

    def update_run(self, run_id, **kwargs):
        self.updated.append({"run_id": run_id, **kwargs})


def _workflow_draft_and_explanation():
    raw = json.loads((FIXTURES / "utility_recovery.json").read_text(encoding="utf-8"))
    project = ProjectInput(
        project_id=raw["project_id"],
        version=raw["project_version"],
        start_offset_workdays=raw["start_offset_workdays"],
        target_finish_offset_workdays=raw["target_finish_offset_workdays"],
        budget_pence=raw["budget_pence"],
        tasks=[
            TaskInput(id=task_id, name=body["name"], duration_workdays=body["duration"], dependency_ids=body["dependencies"])
            for task_id, body in raw["baseline_tasks"].items()
        ],
        catalogue=[
            RecoveryOption(
                option_id=option["option_id"],
                changes=[TaskDurationChange(task_id=c["task_id"], new_duration_workdays=c["new_duration_workdays"]) for c in option["changes"]],
                incremental_cost_pence=option["incremental_cost_pence"],
            )
            for option in raw["recovery_catalogue"]
        ],
    )
    disruption = Disruption(task_id=raw["disruption"]["task_id"], revised_duration_workdays=raw["disruption"]["revised_duration_workdays"])
    evaluation = evaluate_recovery(project, disruption)
    candidate = evaluation.candidate_for(["EXPEDITE_PANEL"])
    draft = DraftRecoveryRequest(
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
    explanation = json.dumps({
        "recommended_candidate_id": candidate.candidate_id,
        "summary": "Expedite restores day 15.",
        "finish_offset_workdays": candidate.finish_offset_workdays,
        "cost_pence": candidate.cost_pence,
    })
    return draft, explanation


def test_concrete_client_sink_maps_types_timestamps_and_parentage():
    client = MockLangSmithClient()
    sink = LangSmithClientSink(client, project_name="review")
    exporter = LangSmithTraceExporter(sink=sink, enabled=True)

    trace = _trace()
    result = exporter.export(trace)

    assert result["exported"] is True
    assert len(client.created) == 2
    parent, child = client.created
    assert parent["run_type"] == "chain"
    assert child["run_type"] == "llm"
    assert isinstance(parent["start_time"], datetime)
    assert parent["start_time"].tzinfo is not None
    assert child["parent_run_id"] == result["run_ids"][trace["spans"][0]["span_id"]]
    assert len(client.updated) == 2
    assert all(isinstance(item["end_time"], datetime) for item in client.updated)


def test_workflow_wires_export_interface_only_when_configured():
    sink = RecordingSink()
    draft, explanation = _workflow_draft_and_explanation()
    provider = FakeProvider([ProviderResponse(content=explanation)])
    workflow = RecoveryWorkflow(
        provider,
        trace_exporter=LangSmithTraceExporter(sink=sink, enabled=True),
    )

    result = workflow.start(draft)

    assert result.status == "completed"
    assert result.trace_export is not None
    assert result.trace_export["exported"] is True
    assert any(item["name"] == "assistant" for item in sink.started)


def test_workflow_default_export_is_absent_and_offline():
    draft, explanation = _workflow_draft_and_explanation()
    provider = FakeProvider([ProviderResponse(content=explanation)])
    result = RecoveryWorkflow(provider).start(draft)

    assert result.status == "completed"
    assert result.trace_export is None

from datetime import datetime, timezone
from delivery_recovery.workflow.exporters import _as_datetime


def test_timestamp_normalisation_accepts_datetime_epoch_and_iso_strings():
    aware = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert _as_datetime(aware) is aware
    assert _as_datetime(aware.timestamp()).tzinfo is not None
    naive = _as_datetime("2026-01-01T00:00:00")
    assert naive.tzinfo is timezone.utc
    fallback = _as_datetime(None)
    assert fallback.tzinfo is not None


def test_multiple_local_roots_get_a_single_synthetic_langsmith_root():
    tracer = TraceRecorder("run-multi", metadata={"source_revision": "rev"})
    with tracer.span("node-a", "node"):
        pass
    with tracer.span("node-b", "node"):
        pass
    sink = RecordingSink()
    exporter = LangSmithTraceExporter(sink=sink, enabled=True)

    result = exporter.export(tracer.export())

    assert result["exported"] is True
    assert sink.started[0]["name"] == "workflow"
    root_id = result["run_ids"]["__root__"]
    assert sink.started[1]["parent_run_id"] == root_id
    assert sink.started[2]["parent_run_id"] == root_id
    assert sink.started[1]["trace_id"] == "run-multi"


def test_synthetic_root_uses_metadata_status_not_unconditional_ok():
    tracer = TraceRecorder(
        "run-degraded",
        metadata={
            "workflow_status": "degraded",
            "reason_code": "INVALID_MODEL_OUTPUT",
            "safe_summary": {"status": "degraded", "approval_state": "not_recorded"},
        },
    )
    with tracer.span("node-a", "node"):
        pass
    with tracer.span("node-b", "node"):
        pass
    sink = RecordingSink()
    exporter = LangSmithTraceExporter(sink=sink, enabled=True)

    result = exporter.export(tracer.export())

    assert result["exported"] is True
    root_start = sink.started[0]
    assert root_start["attributes"]["workflow_status"] == "degraded"
    assert root_start["attributes"]["reason_code"] == "INVALID_MODEL_OUTPUT"
    root_end = next(
        item for item in sink.ended if item["run_id"] == result["run_ids"]["__root__"]
    )
    assert root_end["status"] == "degraded"
    assert root_end["error_type"] is None


def test_safe_summary_and_tool_summaries_are_exported_without_raw_prompts():
    tracer = TraceRecorder(
        "run-safe",
        metadata={
            "workflow_status": "completed",
            "reason_code": None,
            "safe_summary": {
                "status": "completed",
                "recommendation": "CAND-1",
                "baseline_finish": 15,
                "disrupted_finish": 17,
                "recovered_finish": 15,
                "cost_pence": 180000,
                "approval_state": "not_recorded",
                "usage": {"workflow_input_tokens": 108, "workflow_output_tokens": 26},
            },
        },
    )
    with tracer.span(
        "tool:get_schedule_facts",
        "tool",
        tool="get_schedule_facts",
        result_summary={"phase": "disrupted", "finish_offset_workdays": 17},
    ):
        pass
    sink = RecordingSink()
    exporter = LangSmithTraceExporter(sink=sink, enabled=True)

    result = exporter.export(tracer.export())

    assert result["exported"] is True
    item = sink.started[0]
    assert item["metadata"]["safe_summary"]["approval_state"] == "not_recorded"
    assert item["attributes"]["result_summary"]["finish_offset_workdays"] == 17
    serialized = json.dumps(item)
    assert "raw prompt" not in serialized
    assert "api_key" not in serialized


def test_client_sink_maps_native_model_usage_and_safe_outputs():
    tracer = TraceRecorder(
        "run-native",
        metadata={
            "workflow_status": "completed",
            "reason_code": None,
            "ls_provider": "deepseek",
            "ls_model_name": "deepseek-flash",
            "usage_metadata": {
                "input_tokens": 108,
                "output_tokens": 26,
                "total_tokens": 134,
            },
            "safe_summary": {
                "status": "completed",
                "reason_code": None,
                "recommendation": "CAND-1",
                "baseline_finish": 15,
                "disrupted_finish": 17,
                "recovered_finish": 15,
                "cost_pence": 180000,
                "approval_state": "not_recorded",
                "usage": {
                    "workflow_input_tokens": 108,
                    "workflow_output_tokens": 26,
                    "usage_source": "reported",
                    "measured_billing": "unavailable",
                    "estimated_cost_usd": 0.003498,
                },
            },
        },
    )
    with tracer.span("workflow", "node"):
        with tracer.span(
            "provider_call:1",
            "provider",
            ls_provider="deepseek",
            ls_model_name="deepseek-flash",
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
            },
        ):
            pass
        with tracer.span(
            "tool:get_schedule_facts",
            "tool",
            tool="get_schedule_facts",
            result_summary={"phase": "disrupted", "finish_offset_workdays": 17},
        ):
            pass
    client = MockLangSmithClient()
    sink = LangSmithClientSink(client, project_name="review")
    exporter = LangSmithTraceExporter(sink=sink, enabled=True)

    result = exporter.export(tracer.export())

    assert result["exported"] is True
    created = {item["name"]: item for item in client.created}
    assert created["workflow"]["parent_run_id"] is None
    assert created["workflow"]["extra"]["metadata"]["usage_metadata"]["input_tokens"] == 108
    provider = created["provider_call:1"]
    assert provider["run_type"] == "llm"
    assert provider["extra"]["metadata"]["ls_provider"] == "deepseek"
    assert provider["extra"]["metadata"]["ls_model_name"] == "deepseek-flash"
    assert provider["extra"]["metadata"]["usage_metadata"]["input_tokens"] == 100

    updated = {str(item["run_id"]): item for item in client.updated}
    root_output = updated[str(created["workflow"]["id"])]["outputs"]
    assert root_output["recommendation"] == "CAND-1"
    assert root_output["baseline_finish"] == 15
    assert root_output["disrupted_finish"] == 17
    assert root_output["recovered_finish"] == 15
    assert root_output["cost_pence"] == 180000
    assert root_output["approval_state"] == "not_recorded"
    assert root_output["usage"]["workflow_input_tokens"] == 108
    assert root_output["usage"]["measured_billing"] == "unavailable"
    tool_output = updated[str(created["tool:get_schedule_facts"]["id"])]["outputs"]
    assert tool_output["result_summary"]["finish_offset_workdays"] == 17

    serialized = json.dumps(
        {"created": client.created, "updated": client.updated}, default=str
    )
    assert "raw prompt" not in serialized
    assert "api_key" not in serialized


def test_synthetic_root_carries_workflow_usage_only_once():
    tracer = TraceRecorder(
        "run-synthetic-native",
        metadata={
            "workflow_status": "completed",
            "reason_code": None,
            "ls_provider": "deepseek",
            "ls_model_name": "deepseek-flash",
            "usage_metadata": {
                "input_tokens": 108,
                "output_tokens": 26,
                "total_tokens": 134,
            },
            "safe_summary": {"status": "completed", "recommendation": "CAND-1"},
        },
    )
    with tracer.span("node-a", "node"):
        pass
    with tracer.span("node-b", "node"):
        pass
    client = MockLangSmithClient()
    sink = LangSmithClientSink(client, project_name="review")
    exporter = LangSmithTraceExporter(sink=sink, enabled=True)

    result = exporter.export(tracer.export())

    assert result["exported"] is True
    root = next(item for item in client.created if item["parent_run_id"] is None)
    assert root["extra"]["metadata"]["usage_metadata"]["input_tokens"] == 108
    for child in client.created:
        if child["parent_run_id"] is not None:
            assert "usage_metadata" not in child["extra"]["metadata"]
    root_update = next(
        item for item in client.updated if str(item["run_id"]) == str(root["id"])
    )
    assert root_update["outputs"]["recommendation"] == "CAND-1"


def test_validation_error_code_is_exported_as_langsmith_error():
    tracer = TraceRecorder(
        "run-validation",
        metadata={
            "workflow_status": "degraded",
            "reason_code": "INVALID_MODEL_OUTPUT",
        },
    )
    with tracer.span("workflow", "node"):
        with tracer.span(
            "validation",
            "validation",
            error_code="INVALID_MODEL_OUTPUT",
        ):
            pass
    sink = RecordingSink()
    exporter = LangSmithTraceExporter(sink=sink, enabled=True)

    result = exporter.export(tracer.export())

    assert result["exported"] is True
    validation_span = next(
        span for span in tracer.export()["spans"] if span["name"] == "validation"
    )
    validation_run_id = result["run_ids"][validation_span["span_id"]]
    ended = next(item for item in sink.ended if item["run_id"] == validation_run_id)
    assert ended["error_type"] == "INVALID_MODEL_OUTPUT"
