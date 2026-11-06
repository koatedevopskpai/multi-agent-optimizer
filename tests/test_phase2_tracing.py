"""Phase 2 local trace evidence tests.

These are fake-provider traces. They do not establish a live LangSmith export
or real-model integration.
"""

from __future__ import annotations

import json
from pathlib import Path

from delivery_recovery import Disruption, ProjectInput, RecoveryOption, TaskDurationChange, TaskInput, evaluate_recovery
from delivery_recovery.workflow.contracts import DraftRecoveryRequest
from delivery_recovery.workflow.providers import FakeProvider, ProviderResponse, ToolCall
from delivery_recovery.workflow.service import RecoveryWorkflow
from delivery_recovery.workflow.tracing import TraceRecorder

FIXTURES = Path(__file__).parent / "fixtures" / "delivery_recovery"


def _draft_and_explanation():
    raw = json.loads((FIXTURES / "utility_recovery.json").read_text(encoding="utf-8"))
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
    explanation = json.dumps(
        {
            "recommended_candidate_id": candidate.candidate_id,
            "summary": "Expedite restores day 15.",
            "finish_offset_workdays": candidate.finish_offset_workdays,
            "cost_pence": candidate.cost_pence,
        }
    )
    return draft, explanation


def test_trace_has_parent_child_spans_and_correlation_metadata():
    draft, explanation = _draft_and_explanation()
    provider = FakeProvider(
        [
            ProviderResponse(
                tool_calls=[ToolCall(name="get_schedule_facts", arguments={"phase": "disrupted"})]
            ),
            ProviderResponse(content=explanation),
        ]
    )
    workflow = RecoveryWorkflow(
        provider,
        source_revision="trace-test",
        fixture_hash="fixture-123",
    )

    result = workflow.start(draft)
    spans = result.trace["spans"]
    by_id = {span["span_id"]: span for span in spans}

    provider_span = next(span for span in spans if span["name"] == "provider_call:1")
    assistant_span = next(span for span in spans if span["name"] == "assistant")
    tool_span = next(span for span in spans if span["name"] == "tool:get_schedule_facts")

    assert provider_span["parent_id"] == assistant_span["span_id"]
    assert tool_span["parent_id"] == assistant_span["span_id"]
    assert by_id[provider_span["span_id"]]["kind"] == "provider"
    assert result.trace["metadata"]["source_revision"] == "trace-test"
    assert result.trace["metadata"]["fixture_hash"] == "fixture-123"
    assert result.trace["metadata"]["prompt_version"] == "p2-v1"
    assert result.trace["metadata"]["schema_version"] == "explanation-v1"
    assert result.trace["metadata"]["fake_provider"] is True


def test_trace_recorder_nests_spans_and_marks_failures():
    tracer = TraceRecorder("run-1", metadata={"k": "v"})
    with tracer.span("outer", "node"):
        with tracer.span("inner", "tool"):
            pass
        try:
            with tracer.span("bad", "tool"):
                raise RuntimeError("boom")
        except RuntimeError:
            pass

    exported = tracer.export()
    spans = exported["spans"]
    assert spans[0]["name"] == "outer"
    assert spans[1]["parent_id"] == spans[0]["span_id"]
    assert spans[2]["parent_id"] == spans[0]["span_id"]
    assert spans[2]["status"] == "error"
    assert spans[2]["attributes"]["error_type"] == "RuntimeError"


def test_trace_does_not_contain_provider_secrets():
    draft, explanation = _draft_and_explanation()
    provider = FakeProvider([ProviderResponse(content=explanation)])
    workflow = RecoveryWorkflow(provider, source_revision="secret-test")

    result = workflow.start(draft)
    serialized = json.dumps(result.trace)

    assert "sk-" not in serialized
    assert "api_key" not in serialized
    assert "authorization" not in serialized.lower()
