"""Phase 2 conditional workflow, clarification, tools, bounds and degradation."""

from __future__ import annotations

import json
from pathlib import Path

from delivery_recovery import Disruption, ProjectInput, RecoveryOption, TaskDurationChange, TaskInput, evaluate_recovery
from delivery_recovery.workflow.contracts import DraftRecoveryRequest
from delivery_recovery.workflow.live import BudgetedProvider, LiveBudget
from delivery_recovery.workflow.graph import (
    _estimate_tokens,
    _initial_messages,
    _tool_specs,
)
from delivery_recovery.workflow.providers import (
    FakeClock,
    FakeProvider,
    ProviderAuthError,
    ProviderBudgetError,
    ProviderResponse,
    ProviderTimeoutError,
    ToolCall,
)
from delivery_recovery.workflow.service import RecoveryWorkflow, WorkflowLimits

FIXTURES = Path(__file__).parent / "fixtures" / "delivery_recovery"


def _utility_inputs():
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
    return raw, project, disruption


def _draft(**overrides) -> DraftRecoveryRequest:
    raw, project, disruption = _utility_inputs()
    values = {
        "project_id": project.project_id,
        "version": project.version,
        "time_unit": project.time_unit,
        "start_offset_workdays": project.start_offset_workdays,
        "target_finish_offset_workdays": project.target_finish_offset_workdays,
        "budget_pence": project.budget_pence,
        "tasks": project.tasks,
        "catalogue": project.catalogue,
        "disruption": disruption,
    }
    values.update(overrides)
    return DraftRecoveryRequest(**values)


def _evaluation():
    _, project, disruption = _utility_inputs()
    return evaluate_recovery(project, disruption)


def _valid_explanation_json() -> str:
    evaluation = _evaluation()
    candidate = evaluation.candidate_for(["EXPEDITE_PANEL"])
    return json.dumps(
        {
            "recommended_candidate_id": candidate.candidate_id,
            "summary": "Expedite the panel to restore day 15 within budget.",
            "finish_offset_workdays": candidate.finish_offset_workdays,
            "cost_pence": candidate.cost_pence,
        }
    )


def _final_response() -> ProviderResponse:
    return ProviderResponse(content=_valid_explanation_json(), input_tokens=8, output_tokens=6)


def _initial_estimate() -> int:
    return _estimate_tokens(_initial_messages(_evaluation()), _tool_specs())


def test_happy_path_uses_tool_then_validates_explanation():
    provider = FakeProvider(
        [
            ProviderResponse(
                tool_calls=[ToolCall(name="get_schedule_facts", arguments={"phase": "disrupted"})],
                output_tokens=2,
            ),
            _final_response(),
        ]
    )
    workflow = RecoveryWorkflow(
        provider,
        source_revision="test-revision",
        fixture_hash="fixture-hash",
    )

    result = workflow.start(_draft())

    assert result.status == "completed"
    assert result.evaluation is not None
    assert result.evaluation.disrupted.finish_offset_workdays == 17
    assert result.explanation is not None
    assert result.explanation.finish_offset_workdays == 15
    assert result.provider_outcome is not None
    assert result.provider_outcome.mode == "model"
    assert result.provider_outcome.fake_provider is True
    assert len(provider.calls) == 2

    span_names = [span["name"] for span in result.trace["spans"]]
    assert "tool:get_schedule_facts" in span_names
    assert "validation" in span_names
    assert result.trace["metadata"]["source_revision"] == "test-revision"
    assert result.trace["metadata"]["fixture_hash"] == "fixture-hash"
    assert "api_key" not in json.dumps(result.trace)


def test_missing_facts_are_clarified_and_resumed_without_provider_calls():
    draft = _draft(target_finish_offset_workdays=None, budget_pence=None)
    provider = FakeProvider([_final_response()])
    workflow = RecoveryWorkflow(provider)

    first = workflow.start(draft)
    assert first.status == "needs_input"
    assert first.clarification is not None
    assert first.clarification.field == "target_finish_offset_workdays"
    assert provider.calls == []

    second = workflow.resume(first.thread_id, 15)
    assert second.status == "needs_input"
    assert second.clarification is not None
    assert second.clarification.field == "budget_pence"
    assert provider.calls == []

    third = workflow.resume(first.thread_id, 200_000)
    assert third.status == "completed"
    assert third.evaluation is not None
    assert third.evaluation.disrupted.finish_offset_workdays == 17
    assert len(provider.calls) == 1


def test_forbidden_tool_is_rejected_and_never_mutates_domain_facts():
    provider = FakeProvider(
        [
            ProviderResponse(
                tool_calls=[ToolCall(name="delete_dependency", arguments={"task_id": "C"})]
            )
        ]
    )
    workflow = RecoveryWorkflow(provider)

    result = workflow.start(_draft())

    assert result.status == "degraded"
    assert result.evaluation is not None
    assert result.evaluation.disrupted.finish_offset_workdays == 17
    assert result.errors[0].code == "TOOL_NOT_ALLOWED"
    assert len(provider.calls) == 1
    assert any(
        span["name"] == "tool:delete_dependency" and span["status"] == "error"
        for span in result.trace["spans"]
    )


def test_invalid_candidate_in_tool_is_rejected():
    provider = FakeProvider(
        [
            ProviderResponse(
                tool_calls=[
                    ToolCall(
                        name="compare_candidates",
                        arguments={"candidate_ids": ["CAND-does-not-exist"]},
                    )
                ]
            )
        ]
    )
    workflow = RecoveryWorkflow(provider)

    result = workflow.start(_draft())

    assert result.status == "degraded"
    assert result.errors[0].code == "UNKNOWN_CANDIDATE"
    assert result.evaluation is not None
    assert result.evaluation.disrupted.finish_offset_workdays == 17


def test_schema_invalid_output_repairs_once_then_falls_back():
    provider = FakeProvider(
        [ProviderResponse(content="not json"), ProviderResponse(content="still not json")]
    )
    workflow = RecoveryWorkflow(provider)

    result = workflow.start(_draft())

    assert result.status == "degraded"
    assert result.provider_outcome is not None
    assert result.provider_outcome.reason_code == "INVALID_MODEL_OUTPUT"
    assert len(provider.calls) == 2
    assert result.evaluation is not None
    assert result.evaluation.disrupted.finish_offset_workdays == 17
    assert any(span["name"] == "fallback" for span in result.trace["spans"])


def test_semantic_invalid_facts_repair_then_fall_back():
    evaluation = _evaluation()
    candidate = evaluation.candidate_for(["EXPEDITE_PANEL"])
    wrong = json.dumps(
        {
            "recommended_candidate_id": candidate.candidate_id,
            "summary": "Wrong facts.",
            "finish_offset_workdays": 12,
            "cost_pence": 100,
        }
    )
    provider = FakeProvider([ProviderResponse(content=wrong), ProviderResponse(content=wrong)])
    workflow = RecoveryWorkflow(provider)

    result = workflow.start(_draft())

    assert result.status == "degraded"
    assert result.explanation is not None
    assert result.explanation.finish_offset_workdays == 15
    assert result.explanation.cost_pence == 180_000
    assert len(provider.calls) == 2
    assert sum(1 for span in result.trace["spans"] if span["name"] == "validation") >= 2


def test_repair_can_recover_without_fallback():
    provider = FakeProvider([ProviderResponse(content="bad json"), _final_response()])
    workflow = RecoveryWorkflow(provider)

    result = workflow.start(_draft())

    assert result.status == "completed"
    assert result.provider_outcome is not None
    assert result.provider_outcome.mode == "model"
    assert len(provider.calls) == 2


def test_auth_failure_degrades_without_transport_retry():
    provider = FakeProvider([ProviderAuthError("bad key")])
    workflow = RecoveryWorkflow(provider)

    result = workflow.start(_draft())

    assert result.status == "degraded"
    assert result.provider_outcome is not None
    assert result.provider_outcome.reason_code == "PROVIDER_AUTH"
    assert result.provider_outcome.usage_source == "reserved"
    assert result.provider_outcome.transport_attempts == 1
    assert result.provider_outcome.failed_transport_attempts == 1
    assert result.provider_outcome.reserved_tokens > 0
    assert len(provider.calls) == 1
    assert result.evaluation is not None
    assert result.evaluation.baseline.finish_offset_workdays == 15


def test_timeout_failure_is_bounded_and_preserves_facts():
    provider = FakeProvider([ProviderTimeoutError("t1"), ProviderTimeoutError("t2")])
    workflow = RecoveryWorkflow(provider)

    result = workflow.start(_draft())

    assert result.status == "degraded"
    assert result.provider_outcome is not None
    assert result.provider_outcome.reason_code == "PROVIDER_TIMEOUT"
    assert len(provider.calls) == 2
    assert result.evaluation is not None
    assert result.evaluation.disrupted.finish_offset_workdays == 17


def test_execution_step_limit_stops_repeated_tool_calls():
    repeated = [
        ProviderResponse(
            tool_calls=[ToolCall(name="get_schedule_facts", arguments={"phase": "baseline"})]
        )
        for _ in range(6)
    ]
    provider = FakeProvider(repeated)
    workflow = RecoveryWorkflow(
        provider,
        limits=WorkflowLimits(max_steps=2, max_provider_calls=2),
    )

    result = workflow.start(_draft())

    assert result.status == "degraded"
    assert result.provider_outcome is not None
    assert result.provider_outcome.reason_code == "EXECUTION_LIMIT"
    assert len(provider.calls) <= 2
    assert result.evaluation is not None
    assert result.evaluation.disrupted.finish_offset_workdays == 17


def test_token_budget_stops_before_final_explanation():
    provider = FakeProvider(
        [
            ProviderResponse(
                content=_valid_explanation_json(),
                input_tokens=9_000,
                output_tokens=4_000,
            )
        ]
    )
    workflow = RecoveryWorkflow(provider, limits=WorkflowLimits(max_total_tokens=12_000))

    result = workflow.start(_draft())

    assert result.status == "degraded"
    assert result.provider_outcome is not None
    assert result.provider_outcome.reason_code == "TOKEN_BUDGET_EXCEEDED"
    assert len(provider.calls) == 1
    assert result.evaluation is not None
    assert result.evaluation.disrupted.finish_offset_workdays == 17


def test_transport_attempts_are_bounded_across_retries():
    tool_response = ProviderResponse(
        tool_calls=[ToolCall(name="get_schedule_facts", arguments={"phase": "disrupted"})]
    )
    provider = FakeProvider(
        [
            ProviderTimeoutError("first retry"),
            tool_response,
            ProviderTimeoutError("second retry"),
            _final_response(),
        ]
    )
    workflow = RecoveryWorkflow(
        provider,
        limits=WorkflowLimits(max_provider_calls=2),
    )

    result = workflow.start(_draft())

    assert result.status == "degraded"
    assert len(provider.calls) == 2
    assert result.provider_outcome is not None
    assert result.provider_outcome.transport_attempts <= 2


def test_tool_budget_is_cumulative_across_responses():
    tool_response = ProviderResponse(
        tool_calls=[ToolCall(name="get_schedule_facts", arguments={"phase": "disrupted"})]
    )
    provider = FakeProvider([tool_response, tool_response, _final_response()])
    workflow = RecoveryWorkflow(provider, limits=WorkflowLimits(max_tool_calls=1))

    result = workflow.start(_draft())

    assert result.status == "degraded"
    assert result.provider_outcome is not None
    assert result.provider_outcome.reason_code == "TOOL_LIMIT"
    assert sum(1 for span in result.trace["spans"] if span["kind"] == "tool") == 1


def test_late_success_is_rejected_after_response():
    clock = FakeClock(100)

    class LateProvider:
        name = "controlled-late-provider"
        model = "offline"

        def complete(self, request):
            clock.advance(6.0)
            return _final_response()

    workflow = RecoveryWorkflow(
        LateProvider(),
        clock=clock,
        limits=WorkflowLimits(max_elapsed_seconds=5.0),
    )

    result = workflow.start(_draft())

    assert result.status == "degraded"
    assert result.provider_outcome is not None
    assert result.provider_outcome.reason_code == "DEADLINE_EXCEEDED"


def test_token_reservation_rejects_before_call():
    provider = FakeProvider([_final_response()])
    workflow = RecoveryWorkflow(provider, limits=WorkflowLimits(max_total_tokens=10))

    result = workflow.start(_draft())

    assert result.status == "degraded"
    assert result.provider_outcome is not None
    assert result.provider_outcome.reason_code == "TOKEN_BUDGET_EXCEEDED"
    assert result.provider_outcome.transport_attempts == 0
    assert result.provider_outcome.usage_source == "unknown"
    assert result.provider_outcome.workflow_input_tokens == 0
    assert result.provider_outcome.workflow_output_tokens == 0
    assert result.provider_outcome.reserved_tokens == 0
    assert provider.calls == []


def test_missing_usage_is_reported_and_conservatively_counted():
    provider = FakeProvider(
        [
            ProviderResponse(
                content=_valid_explanation_json(),
                usage_reported=False,
            )
        ]
    )
    workflow = RecoveryWorkflow(provider, limits=WorkflowLimits(max_total_tokens=5_000))

    result = workflow.start(_draft())

    assert result.status == "completed"
    assert result.provider_outcome is not None
    assert result.provider_outcome.usage_reported is False
    assert result.provider_outcome.usage_source == "reserved"
    assert result.provider_outcome.reserved_tokens > 0


def test_late_failure_is_rejected_after_response():
    clock = FakeClock(100)

    class LateTimeoutProvider:
        name = "controlled-late-timeout"
        model = "offline"
        calls = 0

        def complete(self, request):
            self.calls += 1
            clock.advance(6.0)
            raise ProviderTimeoutError("late timeout")

    provider = LateTimeoutProvider()
    workflow = RecoveryWorkflow(
        provider,
        clock=clock,
        limits=WorkflowLimits(max_elapsed_seconds=5.0, max_provider_calls=2),
    )

    result = workflow.start(_draft())

    assert result.status == "degraded"
    assert result.provider_outcome is not None
    assert result.provider_outcome.reason_code == "DEADLINE_EXCEEDED"
    assert result.provider_outcome.transport_attempts == 1
    assert result.provider_outcome.failed_transport_attempts == 1
    assert result.provider_outcome.usage_source == "reserved"
    assert result.provider_outcome.workflow_input_tokens == _initial_estimate()
    assert result.provider_outcome.workflow_output_tokens == 1000
    assert result.provider_outcome.reserved_tokens == _initial_estimate() + 1000


def test_zero_repair_budget_falls_back_after_schema_failure():
    provider = FakeProvider([ProviderResponse(content="not json")])
    workflow = RecoveryWorkflow(provider, limits=WorkflowLimits(max_repairs=0))

    result = workflow.start(_draft())

    assert result.status == "degraded"
    assert result.provider_outcome is not None
    assert result.provider_outcome.reason_code == "INVALID_MODEL_OUTPUT"
    assert len(provider.calls) == 1


def test_tool_limit_fallback_preserves_attempt_and_usage_counts():
    provider = FakeProvider(
        [
            ProviderResponse(
                tool_calls=[
                    ToolCall(name="get_schedule_facts", arguments={"phase": "baseline"}),
                    ToolCall(name="get_schedule_facts", arguments={"phase": "disrupted"}),
                ],
                input_tokens=120,
                output_tokens=30,
            )
        ]
    )
    workflow = RecoveryWorkflow(provider, limits=WorkflowLimits(max_tool_calls=1))

    result = workflow.start(_draft())

    assert result.status == "degraded"
    assert result.provider_outcome is not None
    assert result.provider_outcome.reason_code == "TOOL_LIMIT"
    assert result.provider_outcome.transport_attempts == 1
    assert result.provider_outcome.logical_attempts == 1
    assert result.provider_outcome.input_tokens == 120
    assert result.provider_outcome.output_tokens == 30


def test_workflow_usage_aggregates_across_tool_roundtrip():
    provider = FakeProvider(
        [
            ProviderResponse(
                tool_calls=[
                    ToolCall(name="get_schedule_facts", arguments={"phase": "disrupted"})
                ],
                input_tokens=100,
                output_tokens=20,
            ),
            ProviderResponse(
                content=_valid_explanation_json(),
                input_tokens=8,
                output_tokens=6,
            ),
        ]
    )
    workflow = RecoveryWorkflow(provider)

    result = workflow.start(_draft())

    assert result.status == "completed"
    assert result.provider_outcome is not None
    assert result.provider_outcome.input_tokens == 8
    assert result.provider_outcome.output_tokens == 6
    assert result.provider_outcome.workflow_input_tokens == 108
    assert result.provider_outcome.workflow_output_tokens == 26
    assert result.provider_outcome.usage_source == "reported"


def test_invalid_model_output_marks_validation_span_and_root_degraded():
    provider = FakeProvider(
        [ProviderResponse(content="not json"), ProviderResponse(content="still not json")]
    )
    workflow = RecoveryWorkflow(provider)

    result = workflow.start(_draft())

    assert result.status == "degraded"
    assert result.provider_outcome is not None
    assert result.provider_outcome.reason_code == "INVALID_MODEL_OUTPUT"
    assert result.trace["metadata"]["workflow_status"] == "degraded"
    assert result.trace["metadata"]["reason_code"] == "INVALID_MODEL_OUTPUT"
    validation_spans = [
        span for span in result.trace["spans"] if span["name"] == "validation"
    ]
    assert validation_spans
    assert any(
        span["status"] == "error"
        and span["attributes"].get("error_code") == "INVALID_MODEL_OUTPUT"
        for span in validation_spans
    )
    workflow_span = next(
        span for span in result.trace["spans"] if span["name"] == "workflow"
    )
    assert workflow_span["attributes"]["workflow_status"] == "degraded"
    assert workflow_span["attributes"]["reason_code"] == "INVALID_MODEL_OUTPUT"


def test_workflow_usage_source_is_mixed_when_last_call_usage_is_missing():
    provider = FakeProvider(
        [
            ProviderResponse(
                tool_calls=[
                    ToolCall(name="get_schedule_facts", arguments={"phase": "disrupted"})
                ],
                input_tokens=10,
                output_tokens=2,
            ),
            ProviderResponse(
                content=_valid_explanation_json(),
                usage_reported=False,
            ),
        ]
    )
    workflow = RecoveryWorkflow(provider, limits=WorkflowLimits(max_total_tokens=5000))

    result = workflow.start(_draft())

    assert result.status == "completed"
    assert result.provider_outcome is not None
    assert result.provider_outcome.usage_source == "mixed"
    assert result.provider_outcome.workflow_input_tokens > 10
    assert result.provider_outcome.workflow_output_tokens > 2


def test_workflow_tool_summaries_cover_compare_and_explain_tools():
    evaluation = _evaluation()
    expedite = evaluation.candidate_for(["EXPEDITE_PANEL"])
    support = evaluation.candidate_for(["INSTALL_SUPPORT"])
    provider = FakeProvider(
        [
            ProviderResponse(
                tool_calls=[
                    ToolCall(
                        name="compare_candidates",
                        arguments={"candidate_ids": [expedite.candidate_id]},
                    ),
                    ToolCall(
                        name="explain_constraint",
                        arguments={"candidate_id": support.candidate_id},
                    ),
                ]
            ),
            ProviderResponse(content=_valid_explanation_json()),
        ]
    )
    workflow = RecoveryWorkflow(provider)

    result = workflow.start(_draft())

    tool_spans = {
        span["name"]: span for span in result.trace["spans"] if span["kind"] == "tool"
    }
    assert tool_spans["tool:compare_candidates"]["attributes"]["result_summary"]["candidate_ids"] == [
        expedite.candidate_id
    ]
    assert tool_spans["tool:explain_constraint"]["attributes"]["result_summary"][
        "rejection_codes"
    ] == ["TARGET_MISSED"]


def test_timeout_then_success_accounts_failed_reservation_and_reported_usage():
    estimate = _initial_estimate()
    provider = FakeProvider([ProviderTimeoutError("retry"), _final_response()])
    workflow = RecoveryWorkflow(provider)

    result = workflow.start(_draft())

    assert result.status == "completed"
    assert result.provider_outcome is not None
    outcome = result.provider_outcome
    assert outcome.transport_attempts == 2
    assert outcome.failed_transport_attempts == 1
    assert outcome.reported_transport_attempts == 1
    assert outcome.reserved_transport_attempts == 1
    assert outcome.workflow_input_tokens == estimate + 8
    assert outcome.workflow_output_tokens == 1000 + 6
    assert outcome.measured_input_tokens == 8
    assert outcome.measured_output_tokens == 6
    assert outcome.reserved_input_tokens == estimate
    assert outcome.reserved_output_tokens == 1000
    assert outcome.usage_source == "mixed"
    assert outcome.reserved_tokens == 2 * (estimate + 1000)
    assert [item.usage_source for item in outcome.transport_attempt_usage] == [
        "reserved",
        "reported",
    ]
    assert outcome.transport_attempt_usage[0].input_tokens == estimate
    assert outcome.transport_attempt_usage[0].output_tokens == 1000
    assert outcome.transport_attempt_usage[1].input_tokens == 8
    assert outcome.transport_attempt_usage[1].output_tokens == 6
    safe_usage = result.trace["metadata"]["safe_summary"]["usage"]
    assert safe_usage["transport_attempts"] == 2
    assert safe_usage["failed_transport_attempts"] == 1
    assert [item["usage_source"] for item in safe_usage["attempt_usage"]] == [
        "reserved",
        "reported",
    ]


def test_exhausted_retries_reserve_every_transport_attempt():
    estimate = _initial_estimate()
    provider = FakeProvider(
        [ProviderTimeoutError("first"), ProviderTimeoutError("second")]
    )
    workflow = RecoveryWorkflow(provider)

    result = workflow.start(_draft())

    assert result.status == "degraded"
    assert result.provider_outcome is not None
    outcome = result.provider_outcome
    assert outcome.reason_code == "PROVIDER_TIMEOUT"
    assert outcome.transport_attempts == 2
    assert outcome.failed_transport_attempts == 2
    assert outcome.workflow_input_tokens == 2 * estimate
    assert outcome.workflow_output_tokens == 2 * 1000
    assert outcome.measured_input_tokens == 0
    assert outcome.measured_output_tokens == 0
    assert outcome.reserved_input_tokens == 2 * estimate
    assert outcome.reserved_output_tokens == 2 * 1000
    assert outcome.usage_source == "reserved"
    assert outcome.reserved_tokens == 2 * (estimate + 1000)
    assert [item.usage_source for item in outcome.transport_attempt_usage] == [
        "reserved",
        "reserved",
    ]


def test_retry_is_rejected_before_transport_when_capacity_is_insufficient():
    estimate = _initial_estimate()
    one_reservation = estimate + 1000
    provider = FakeProvider([ProviderTimeoutError("first"), _final_response()])
    workflow = RecoveryWorkflow(
        provider,
        limits=WorkflowLimits(max_total_tokens=one_reservation + 1),
    )

    result = workflow.start(_draft())

    assert result.status == "degraded"
    assert result.provider_outcome is not None
    outcome = result.provider_outcome
    assert outcome.reason_code == "TOKEN_BUDGET_EXCEEDED"
    assert outcome.transport_attempts == 1
    assert outcome.failed_transport_attempts == 1
    assert len(provider.calls) == 1
    assert outcome.workflow_input_tokens == estimate
    assert outcome.workflow_output_tokens == 1000
    assert outcome.usage_source == "reserved"
    assert outcome.reserved_tokens == one_reservation
    assert len(outcome.transport_attempt_usage) == 1


def test_late_response_retains_usage_while_workflow_rejects_it():
    clock = FakeClock(100)

    class LateSuccessProvider:
        name = "controlled-late-success"
        model = "offline"

        def complete(self, request):
            clock.advance(6.0)
            return _final_response()

    workflow = RecoveryWorkflow(
        LateSuccessProvider(),
        clock=clock,
        limits=WorkflowLimits(max_elapsed_seconds=5.0, max_provider_calls=1),
    )

    result = workflow.start(_draft())

    assert result.status == "degraded"
    assert result.provider_outcome is not None
    outcome = result.provider_outcome
    assert outcome.reason_code == "DEADLINE_EXCEEDED"
    assert outcome.transport_attempts == 1
    assert outcome.reported_transport_attempts == 1
    assert outcome.workflow_input_tokens == 8
    assert outcome.workflow_output_tokens == 6
    assert outcome.usage_source == "reported"


def test_circuit_open_rejection_does_not_consume_a_transport_attempt():
    provider = FakeProvider(
        [
            ProviderTimeoutError("one"),
            ProviderTimeoutError("two"),
            ProviderTimeoutError("three"),
        ]
    )
    workflow = RecoveryWorkflow(provider)

    first = workflow.start(_draft())
    second = workflow.start(_draft())
    blocked = workflow.start(_draft())

    assert first.provider_outcome is not None
    assert second.provider_outcome is not None
    assert blocked.provider_outcome is not None
    assert second.provider_outcome.reason_code == "CIRCUIT_OPEN"
    assert second.provider_outcome.transport_attempts == 1
    assert blocked.provider_outcome.reason_code == "CIRCUIT_OPEN"
    assert blocked.provider_outcome.transport_attempts == 0
    assert blocked.provider_outcome.workflow_input_tokens == 0
    assert blocked.provider_outcome.workflow_output_tokens == 0
    assert blocked.provider_outcome.measured_input_tokens == 0
    assert blocked.provider_outcome.measured_output_tokens == 0
    assert blocked.provider_outcome.reserved_input_tokens == 0
    assert blocked.provider_outcome.reserved_output_tokens == 0
    assert blocked.provider_outcome.usage_source == "unknown"
    assert blocked.provider_outcome.reserved_tokens == 0
    assert blocked.provider_outcome.transport_attempt_usage == []
    assert len(provider.calls) == 3


def test_exhausted_session_attempt_budget_is_not_a_workflow_transport():
    inner = FakeProvider([_final_response()])
    budget = LiveBudget(max_attempts=1, attempts=1)
    workflow = RecoveryWorkflow(BudgetedProvider(inner, budget))

    result = workflow.start(_draft())

    assert result.status == "degraded"
    assert result.provider_outcome is not None
    outcome = result.provider_outcome
    assert outcome.reason_code == "BUDGET_EXCEEDED"
    assert len(inner.calls) == 0
    assert budget.attempts == 1
    assert outcome.transport_attempts == 0
    assert outcome.failed_transport_attempts == 0
    assert outcome.workflow_input_tokens == 0
    assert outcome.workflow_output_tokens == 0
    assert outcome.reserved_input_tokens == 0
    assert outcome.reserved_output_tokens == 0
    assert outcome.reserved_tokens == 0
    assert outcome.usage_source == "unknown"
    assert outcome.transport_attempt_usage == []


def test_session_input_token_rejection_is_not_a_workflow_transport():
    inner = FakeProvider([_final_response()])
    budget = LiveBudget(max_attempts=10, max_input_tokens=1)
    workflow = RecoveryWorkflow(BudgetedProvider(inner, budget))

    result = workflow.start(_draft())

    assert result.status == "degraded"
    assert result.provider_outcome is not None
    outcome = result.provider_outcome
    assert outcome.reason_code == "BUDGET_EXCEEDED"
    assert len(inner.calls) == 0
    assert budget.attempts == 0
    assert outcome.transport_attempts == 0
    assert outcome.workflow_input_tokens == 0
    assert outcome.workflow_output_tokens == 0
    assert outcome.reserved_tokens == 0
    assert outcome.usage_source == "unknown"
    assert outcome.transport_attempt_usage == []


def test_session_output_token_rejection_is_not_a_workflow_transport():
    inner = FakeProvider([_final_response()])
    budget = LiveBudget(max_attempts=10, max_output_tokens_per_request=1)
    workflow = RecoveryWorkflow(BudgetedProvider(inner, budget))

    result = workflow.start(_draft())

    assert result.status == "degraded"
    assert result.provider_outcome is not None
    outcome = result.provider_outcome
    assert outcome.reason_code == "BUDGET_EXCEEDED"
    assert len(inner.calls) == 0
    assert budget.attempts == 0
    assert outcome.transport_attempts == 0
    assert outcome.workflow_input_tokens == 0
    assert outcome.workflow_output_tokens == 0
    assert outcome.reserved_tokens == 0
    assert outcome.usage_source == "unknown"
    assert outcome.transport_attempt_usage == []


def test_session_cost_rejection_is_not_a_workflow_transport():
    inner = FakeProvider([_final_response()])
    budget = LiveBudget(max_attempts=10, max_cost_gbp=0.0)
    workflow = RecoveryWorkflow(BudgetedProvider(inner, budget))

    result = workflow.start(_draft())

    assert result.status == "degraded"
    assert result.provider_outcome is not None
    outcome = result.provider_outcome
    assert outcome.reason_code == "BUDGET_EXCEEDED"
    assert len(inner.calls) == 0
    assert budget.attempts == 0
    assert outcome.transport_attempts == 0
    assert outcome.workflow_input_tokens == 0
    assert outcome.workflow_output_tokens == 0
    assert outcome.reserved_tokens == 0
    assert outcome.usage_source == "unknown"
    assert outcome.transport_attempt_usage == []


def test_timeout_then_session_budget_rejection_retains_first_attempt():
    estimate = _initial_estimate()
    inner = FakeProvider([ProviderTimeoutError("first")])
    budget = LiveBudget(max_attempts=1)
    workflow = RecoveryWorkflow(BudgetedProvider(inner, budget))

    result = workflow.start(_draft())

    assert result.status == "degraded"
    assert result.provider_outcome is not None
    outcome = result.provider_outcome
    assert outcome.reason_code == "BUDGET_EXCEEDED"
    assert len(inner.calls) == 1
    assert budget.attempts == 1
    assert outcome.transport_attempts == 1
    assert outcome.failed_transport_attempts == 1
    assert outcome.workflow_input_tokens == estimate
    assert outcome.workflow_output_tokens == 1000
    assert outcome.reserved_input_tokens == estimate
    assert outcome.reserved_output_tokens == 1000
    assert outcome.reserved_tokens == estimate + 1000
    assert outcome.usage_source == "reserved"
    assert len(outcome.transport_attempt_usage) == 1
    assert budget.summary()["records"][0]["usage_source"] == "reserved_on_failure"


def test_admitted_transport_failure_still_consumes_reservation_with_session_budget():
    estimate = _initial_estimate()
    inner = FakeProvider(
        [ProviderTimeoutError("one"), ProviderTimeoutError("two")]
    )
    budget = LiveBudget(max_attempts=2)
    workflow = RecoveryWorkflow(BudgetedProvider(inner, budget))

    result = workflow.start(_draft())

    assert result.status == "degraded"
    assert result.provider_outcome is not None
    outcome = result.provider_outcome
    assert outcome.reason_code == "PROVIDER_TIMEOUT"
    assert len(inner.calls) == 2
    assert budget.attempts == 2
    assert outcome.transport_attempts == 2
    assert outcome.failed_transport_attempts == 2
    assert outcome.workflow_input_tokens == 2 * estimate
    assert outcome.workflow_output_tokens == 2 * 1000
    assert outcome.reserved_tokens == 2 * (estimate + 1000)
    assert outcome.usage_source == "reserved"
    assert [record["usage_source"] for record in budget.summary()["records"]] == [
        "reserved_on_failure",
        "reserved_on_failure",
    ]


def test_inner_budget_error_after_admission_still_consumes_attempt():
    estimate = _initial_estimate()

    class InnerBudgetFailure:
        name = "inner-budget"
        model = "offline"

        def complete(self, request):
            raise ProviderBudgetError("budget failed after transport started")

    workflow = RecoveryWorkflow(InnerBudgetFailure())

    result = workflow.start(_draft())

    assert result.status == "degraded"
    assert result.provider_outcome is not None
    outcome = result.provider_outcome
    assert outcome.reason_code == "BUDGET_EXCEEDED"
    assert outcome.transport_attempts == 1
    assert outcome.failed_transport_attempts == 1
    assert outcome.workflow_input_tokens == estimate
    assert outcome.workflow_output_tokens == 1000
    assert outcome.reserved_tokens == estimate + 1000
    assert outcome.usage_source == "reserved"
    assert len(outcome.transport_attempt_usage) == 1
