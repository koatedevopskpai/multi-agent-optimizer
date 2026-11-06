"""Typed conditional LangGraph workflow for delivery recovery.

The graph is deliberately small:

    intake -> compute -> assistant -> validate -> finalize
                             ^            |
                             +-- repair --+
                             |
                             +--> fallback -> finalize

Human clarification pauses at an ``interrupt`` and resumes through the same
thread. No approval transaction, external project mutation or persistent
decision is implemented.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from ..candidates import evaluate_recovery
from ..contracts import RecoveryEvaluation
from .contracts import (
    ApprovalRequest,
    DraftRecoveryRequest,
    Explanation,
    ProviderOutcome,
    TransportAttemptUsage,
    WorkflowError,
)
from .providers import (
    CallOutcome,
    ChatMessage,
    FakeProvider,
    ProviderRequest,
    ProviderRunner,
    ProviderResponse,
    ToolCall,
)
from .tools import TOOL_SPECS, ToolError, execute_tool
from .tracing import TraceRecorder
from .validation import deterministic_explanation, render_explanation, validate_explanation

__all__ = ["WorkflowState", "build_workflow"]

SYSTEM_PROMPT = (
    "You are a bounded delivery-recovery assistant. The deterministic CPM "
    "facts are authoritative. Use only the allowlisted read-only tools. Never "
    "invent durations, costs, candidate IDs or approvals, and never alter the "
    "project. Return one JSON object with recommended_candidate_id, summary, "
    "finish_offset_workdays and cost_pence. If no candidate is feasible, use "
    "recommended_candidate_id=null and omit finish and cost."
)


class WorkflowState(TypedDict, total=False):
    draft: dict
    project: dict
    evaluation: dict
    explanation: dict
    provider_outcome: dict
    status: str
    errors: list[dict]
    messages: list[dict]
    steps: int
    provider_calls: int
    total_tokens: int
    reserved_tokens: int
    workflow_input_tokens: int
    workflow_output_tokens: int
    usage_reported: bool
    usage_sources: list[str]
    transport_attempts: int
    transport_attempt_usage: list[dict]
    repairs: int
    repair_errors: list[str]
    last_call: dict
    turn_started: float
    fallback_reason: str
    pending_decision: dict
    decision_id: str


def _tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.arguments_model.model_json_schema(),
            "read_only": spec.read_only,
        }
        for spec in TOOL_SPECS.values()
    ]


def _user_prompt(evaluation: RecoveryEvaluation) -> str:
    return json.dumps(
        {
            "task": "Explain the deterministic recommendation and rejections.",
            "baseline": evaluation.baseline.model_dump(mode="json"),
            "disrupted": evaluation.disrupted.model_dump(mode="json"),
            "candidates": [candidate.model_dump(mode="json") for candidate in evaluation.candidates],
            "recommended_candidate_id": evaluation.recommended_candidate_id,
        },
        sort_keys=True,
    )


def _initial_messages(evaluation: RecoveryEvaluation) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _user_prompt(evaluation)},
    ]


def _estimate_tokens(messages: list[dict], tools: list[dict], *, chars_per_token: int = 4) -> int:
    """Conservative pre-call token estimate.

    This is an admission estimate, not a tokenizer guarantee. The limitation is
    documented; reported usage reconciles it after the call, and missing usage
    is charged at this reservation.
    """

    text = json.dumps({"messages": messages, "tools": tools}, sort_keys=True)
    return max(1, len(text) // chars_per_token)


def _safe_tool_summary(name: str, result: dict[str, Any]) -> dict[str, Any]:
    if name == "get_schedule_facts":
        return {
            "phase": result.get("phase"),
            "finish_offset_workdays": result.get("finish_offset_workdays"),
            "critical_task_count": len(result.get("critical_task_ids") or []),
        }
    if name == "compare_candidates":
        return {
            "candidate_ids": [
                item.get("candidate_id") for item in result.get("candidates", [])
            ]
        }
    if name == "explain_constraint":
        return {
            "candidate_id": result.get("candidate_id"),
            "feasible": result.get("feasible"),
            "rejection_codes": result.get("rejection_codes"),
        }
    return {"keys": sorted(result)}


def _call_to_dict(call: CallOutcome) -> dict[str, Any]:
    return {
        "ok": call.ok,
        "reason_code": call.reason_code,
        "transport_attempts": call.transport_attempts,
        "logical_attempts": call.logical_attempts,
        "elapsed_ms": call.elapsed_ms,
        "provider": call.provider,
        "model": call.model,
    }


def _attempt_token_sums(
    attempt_usage: list[TransportAttemptUsage],
) -> tuple[int, int, int, int]:
    measured_input = sum(
        item.input_tokens for item in attempt_usage if item.usage_source == "reported"
    )
    measured_output = sum(
        item.output_tokens for item in attempt_usage if item.usage_source == "reported"
    )
    reserved_input = sum(
        item.input_tokens for item in attempt_usage if item.usage_source == "reserved"
    )
    reserved_output = sum(
        item.output_tokens for item in attempt_usage if item.usage_source == "reserved"
    )
    return measured_input, measured_output, reserved_input, reserved_output


def _provider_outcome(
    call: CallOutcome,
    *,
    fake_provider: bool,
    mode: str = "model",
    reason_code: str | None = None,
    transport_attempts: int | None = None,
    logical_attempts: int | None = None,
    reserved_tokens: int = 0,
    workflow_input_tokens: int = 0,
    workflow_output_tokens: int = 0,
    usage_source: str = "unknown",
    transport_attempt_usage: list[dict] | None = None,
) -> ProviderOutcome:
    response = call.response
    attempt_usage = [
        TransportAttemptUsage.model_validate(item)
        for item in (transport_attempt_usage or [])
    ]
    (
        measured_input,
        measured_output,
        reserved_input,
        reserved_output,
    ) = _attempt_token_sums(attempt_usage)
    return ProviderOutcome(
        mode=mode,  # type: ignore[arg-type]
        reason_code=reason_code if reason_code is not None else call.reason_code,
        logical_attempts=(
            call.logical_attempts if logical_attempts is None else logical_attempts
        ),
        transport_attempts=(
            call.transport_attempts
            if transport_attempts is None
            else transport_attempts
        ),
        failed_transport_attempts=sum(1 for item in attempt_usage if not item.ok),
        reported_transport_attempts=sum(
            1 for item in attempt_usage if item.usage_source == "reported"
        ),
        reserved_transport_attempts=sum(
            1 for item in attempt_usage if item.usage_source == "reserved"
        ),
        elapsed_ms=call.elapsed_ms,
        provider=call.provider,
        model=call.model,
        input_tokens=response.input_tokens if response else 0,
        output_tokens=response.output_tokens if response else 0,
        workflow_input_tokens=workflow_input_tokens,
        workflow_output_tokens=workflow_output_tokens,
        measured_input_tokens=measured_input,
        measured_output_tokens=measured_output,
        reserved_input_tokens=reserved_input,
        reserved_output_tokens=reserved_output,
        reserved_tokens=reserved_tokens,
        usage_reported=response.usage_reported if response else False,
        usage_source=usage_source,
        transport_attempt_usage=attempt_usage,
        fake_provider=fake_provider,
    )


def _fallback_state(
    state: WorkflowState,
    *,
    code: str,
    message: str,
    call: CallOutcome | None = None,
    fake_provider: bool,
    provider_name: str,
    provider_model: str,
) -> dict[str, Any]:
    errors = list(state.get("errors") or [])
    errors.append(WorkflowError(code=code, message=message).model_dump(mode="json"))

    last = state.get("last_call") or {}
    transport_total = int(
        state.get("transport_attempts", last.get("transport_attempts", 0))
    )
    logical_total = int(state.get("provider_calls", last.get("logical_attempts", 0)))
    reserved_total = int(state.get("reserved_tokens", last.get("reserved_tokens", 0)))
    usage_reported = bool(state.get("usage_reported", last.get("usage_reported", False)))
    workflow_input_total = int(state.get("workflow_input_tokens", 0))
    workflow_output_total = int(state.get("workflow_output_tokens", 0))
    attempt_usage_raw = list(state.get("transport_attempt_usage") or [])
    attempt_usage = [
        TransportAttemptUsage.model_validate(item) for item in attempt_usage_raw
    ]
    sources = list(state.get("usage_sources") or [])
    if sources and all(source == "reported" for source in sources):
        usage_source = "reported"
    elif sources and all(source == "reserved" for source in sources):
        usage_source = "reserved"
    elif sources:
        usage_source = "mixed"
    else:
        usage_source = "unknown"

    if call is not None:
        outcome = _provider_outcome(
            call,
            fake_provider=fake_provider,
            mode="deterministic_fallback",
            reason_code=code,
            transport_attempts=transport_total,
            logical_attempts=logical_total,
            reserved_tokens=reserved_total,
            workflow_input_tokens=workflow_input_total,
            workflow_output_tokens=workflow_output_total,
            usage_source=usage_source,
            transport_attempt_usage=attempt_usage_raw,
        ).model_copy(update={"usage_reported": usage_reported})
    else:
        (
            measured_input,
            measured_output,
            reserved_input,
            reserved_output,
        ) = _attempt_token_sums(attempt_usage)
        outcome = ProviderOutcome(
            mode="deterministic_fallback",
            reason_code=code,
            logical_attempts=logical_total,
            transport_attempts=transport_total,
            failed_transport_attempts=sum(1 for item in attempt_usage if not item.ok),
            reported_transport_attempts=sum(
                1 for item in attempt_usage if item.usage_source == "reported"
            ),
            reserved_transport_attempts=sum(
                1 for item in attempt_usage if item.usage_source == "reserved"
            ),
            elapsed_ms=int(last.get("elapsed_ms", 0)),
            provider=str(last.get("provider", provider_name)),
            model=str(last.get("model", provider_model)),
            workflow_input_tokens=workflow_input_total,
            workflow_output_tokens=workflow_output_total,
            measured_input_tokens=measured_input,
            measured_output_tokens=measured_output,
            reserved_input_tokens=reserved_input,
            reserved_output_tokens=reserved_output,
            reserved_tokens=reserved_total,
            usage_reported=usage_reported,
            usage_source=usage_source,
            transport_attempt_usage=attempt_usage,
            fake_provider=fake_provider,
        )

    return {
        "status": "fallback",
        "fallback_reason": code,
        "provider_outcome": outcome.model_dump(mode="json"),
        "errors": errors,
        "last_call": state.get("last_call"),
        "steps": int(state.get("steps", 0)),
        "provider_calls": int(state.get("provider_calls", 0)),
        "transport_attempts": int(state.get("transport_attempts", 0)),
        "tool_calls": int(state.get("tool_calls", 0)),
        "total_tokens": int(state.get("total_tokens", 0)),
        "workflow_input_tokens": int(state.get("workflow_input_tokens", 0)),
        "workflow_output_tokens": int(state.get("workflow_output_tokens", 0)),
        "reserved_tokens": int(state.get("reserved_tokens", 0)),
        "usage_reported": bool(state.get("usage_reported", False)),
        "usage_sources": list(state.get("usage_sources") or []),
        "transport_attempt_usage": list(
            state.get("transport_attempt_usage") or []
        ),
    }


def build_workflow(
    runner: ProviderRunner,
    tracer: TraceRecorder,
    *,
    limits,
    fake_provider: bool,
    provider_name: str,
    provider_model: str,
    clock: Callable[[], float] | None = None,
    require_approval: bool = False,
    decision_resolver: Callable[[str], dict[str, Any] | None] | None = None,
):
    """Build and compile the workflow graph."""

    now = clock or time.monotonic

    def intake(state: WorkflowState) -> dict[str, Any]:
        with tracer.span("intake", "node") as span:
            try:
                draft = DraftRecoveryRequest.model_validate(state.get("draft") or {})
            except ValidationError as exc:
                span.attributes["error_count"] = len(exc.errors())
                return {
                    "status": "failed",
                    "errors": [
                        WorkflowError(
                            code="INVALID_DRAFT",
                            message="The draft contains invalid supplied values.",
                        ).model_dump(mode="json")
                    ],
                }

            while True:
                missing = draft.missing_facts()
                if not missing:
                    break
                fact = missing[0]
                span.attributes.setdefault("clarifications", []).append(fact.field)
                answer = interrupt({"field": fact.field, "question": fact.question})
                try:
                    draft = draft.with_answer(fact.field, answer)
                except ValidationError:
                    return {
                        "status": "failed",
                        "errors": [
                            WorkflowError(
                                code="INVALID_CLARIFICATION",
                                message=f"The answer for {fact.field!r} is not valid.",
                            ).model_dump(mode="json")
                        ],
                    }
            return {"draft": draft.model_dump(mode="json")}

    def compute(state: WorkflowState) -> dict[str, Any]:
        with tracer.span("compute", "node"):
            draft = DraftRecoveryRequest.model_validate(state["draft"])
            project = draft.to_project_input()
            evaluation = evaluate_recovery(project, draft.disruption)
            return {
                "project": project.model_dump(mode="json"),
                "evaluation": evaluation.model_dump(mode="json"),
            }

    def assistant(state: WorkflowState) -> dict[str, Any]:
        with tracer.span("assistant", "node"):
            evaluation = RecoveryEvaluation.model_validate(state["evaluation"])
            messages = list(state.get("messages") or _initial_messages(evaluation))
            steps = int(state.get("steps", 0))
            calls = int(state.get("provider_calls", 0))
            transport_attempts = int(state.get("transport_attempts", 0))
            executed_tools = int(state.get("tool_calls", 0))
            workflow_input_tokens = int(state.get("workflow_input_tokens", 0))
            workflow_output_tokens = int(state.get("workflow_output_tokens", 0))
            tokens = int(state.get("total_tokens", workflow_input_tokens + workflow_output_tokens))
            reserved_tokens = int(state.get("reserved_tokens", 0))
            repairs = int(state.get("repairs", 0))
            usage_reported = bool(state.get("usage_reported", False))
            usage_sources = list(state.get("usage_sources") or [])
            attempt_usage = list(state.get("transport_attempt_usage") or [])
            turn_started = float(state.get("turn_started") or now())
            repair_errors = list(state.get("repair_errors") or [])
            last_call = state.get("last_call")

            if repair_errors:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Validation failed with: "
                            + "; ".join(repair_errors)
                            + ". Return corrected JSON using only the verified facts."
                        ),
                    }
                )

            def current(extra: dict[str, Any] | None = None) -> dict[str, Any]:
                snapshot = {
                    "steps": steps,
                    "provider_calls": calls,
                    "transport_attempts": transport_attempts,
                    "tool_calls": executed_tools,
                    "total_tokens": tokens,
                    "workflow_input_tokens": workflow_input_tokens,
                    "workflow_output_tokens": workflow_output_tokens,
                    "reserved_tokens": reserved_tokens,
                    "usage_reported": usage_reported,
                    "usage_sources": usage_sources,
                    "transport_attempt_usage": list(attempt_usage),
                    "turn_started": turn_started,
                    "last_call": last_call,
                }
                if extra:
                    snapshot.update(extra)
                return snapshot

            while steps < limits.max_steps:
                elapsed = now() - turn_started
                if elapsed >= limits.max_elapsed_seconds:
                    return _fallback_state(
                        {**state, **current()},
                        code="DEADLINE_EXCEEDED",
                        message="The active whole-turn deadline was exceeded.",
                        fake_provider=fake_provider,
                        provider_name=provider_name,
                        provider_model=provider_model,
                    )

                remaining_transport = limits.max_provider_calls - transport_attempts
                if remaining_transport <= 0:
                    return _fallback_state(
                        {**state, **current()},
                        code="EXECUTION_LIMIT",
                        message="The whole-turn transport-call budget was exhausted.",
                        fake_provider=fake_provider,
                        provider_name=provider_name,
                        provider_model=provider_model,
                    )

                tool_specs = _tool_specs()
                estimate = _estimate_tokens(messages, tool_specs)
                reservation = estimate + limits.max_output_tokens
                if tokens + reservation > limits.max_total_tokens:
                    return _fallback_state(
                        {**state, **current()},
                        code="TOKEN_BUDGET_EXCEEDED",
                        message="The token reservation would exceed the whole-turn budget.",
                        fake_provider=fake_provider,
                        provider_name=provider_name,
                        provider_model=provider_model,
                    )
                attempt_reservations: list[int] = []

                def admit_attempt() -> str | None:
                    projected = tokens + sum(attempt_reservations) + reservation
                    if projected > limits.max_total_tokens:
                        return "TOKEN_BUDGET_EXCEEDED"
                    attempt_reservations.append(reservation)
                    return None

                remaining_seconds = max(0.001, limits.max_elapsed_seconds - elapsed)

                request = ProviderRequest(
                    messages=[ChatMessage(**message) for message in messages],
                    tools=tool_specs,
                    max_output_tokens=limits.max_output_tokens,
                    run_id=tracer.run_id,
                    deadline_at=turn_started + limits.max_elapsed_seconds,
                    remaining_seconds=remaining_seconds,
                )
                with tracer.span(
                    f"provider_call:{calls + 1}",
                    "provider",
                    attempt=calls + 1,
                    fake_provider=fake_provider,
                    reserved_tokens=reservation,
                    remaining_seconds=round(remaining_seconds, 6),
                ) as provider_span:
                    call = runner.complete(
                        request,
                        max_transport_calls=min(2, remaining_transport),
                        before_attempt=admit_attempt,
                    )
                    # A provider-level admission rejection happens after the
                    # workflow guard reserved capacity but before transport
                    # starts. Keep only reservations that correspond to actual
                    # attempts.
                    del attempt_reservations[call.transport_attempts :]
                    calls += 1
                    steps += 1
                    transport_attempts += call.transport_attempts
                    reserved_tokens += sum(attempt_reservations)
                    last_call = _call_to_dict(call)

                    response = call.response if call.ok else None
                    failed_attempts = len(attempt_reservations) - (
                        1 if response is not None else 0
                    )
                    new_attempt_usage: list[TransportAttemptUsage] = []
                    for index in range(max(0, failed_attempts)):
                        new_attempt_usage.append(
                            TransportAttemptUsage(
                                call=calls,
                                attempt=index + 1,
                                ok=False,
                                usage_source="reserved",
                                input_tokens=estimate,
                                output_tokens=limits.max_output_tokens,
                                reservation_tokens=attempt_reservations[index],
                            )
                        )
                    if response is not None:
                        if response.usage_reported:
                            source = "reported"
                            attempt_input = response.input_tokens
                            attempt_output = response.output_tokens
                        else:
                            source = "reserved"
                            attempt_input = estimate
                            attempt_output = limits.max_output_tokens
                        new_attempt_usage.append(
                            TransportAttemptUsage(
                                call=calls,
                                attempt=len(attempt_reservations),
                                ok=True,
                                usage_source=source,
                                input_tokens=attempt_input,
                                output_tokens=attempt_output,
                                reservation_tokens=(
                                    attempt_reservations[-1]
                                    if attempt_reservations
                                    else 0
                                ),
                            )
                        )

                    for item in new_attempt_usage:
                        workflow_input_tokens += item.input_tokens
                        workflow_output_tokens += item.output_tokens
                        usage_sources.append(item.usage_source)
                    attempt_usage.extend(
                        item.model_dump(mode="json") for item in new_attempt_usage
                    )
                    tokens = workflow_input_tokens + workflow_output_tokens
                    usage_reported = bool(usage_sources) and all(
                        source == "reported" for source in usage_sources
                    )

                    provider_span.attributes["ls_provider"] = provider_name
                    provider_span.attributes["ls_model_name"] = provider_model
                    provider_span.attributes["reserved_tokens"] = sum(
                        attempt_reservations
                    )
                    provider_span.attributes["transport_attempts"] = (
                        call.transport_attempts
                    )
                    provider_span.attributes["attempt_usage"] = [
                        item.model_dump(mode="json") for item in new_attempt_usage
                    ]
                    if response is not None and response.usage_reported:
                        provider_span.attributes["usage_metadata"] = {
                            "input_tokens": response.input_tokens,
                            "output_tokens": response.output_tokens,
                            "total_tokens": (
                                response.input_tokens + response.output_tokens
                            ),
                            "source": "reported",
                        }
                    elif attempt_reservations:
                        provider_span.attributes["usage_metadata"] = {
                            "input_tokens": None,
                            "output_tokens": None,
                            "total_tokens": None,
                            "source": "reserved",
                            "reserved_tokens": sum(attempt_reservations),
                        }
                    else:
                        provider_span.attributes["usage_metadata"] = {
                            "input_tokens": None,
                            "output_tokens": None,
                            "total_tokens": None,
                            "source": "unknown",
                        }

                if now() - turn_started >= limits.max_elapsed_seconds:
                    return _fallback_state(
                        {**state, **current()},
                        code="DEADLINE_EXCEEDED",
                        message="The provider response arrived after the whole-turn deadline.",
                        call=call,
                        fake_provider=fake_provider,
                        provider_name=provider_name,
                        provider_model=provider_model,
                    )

                if not call.ok or call.response is None:
                    return _fallback_state(
                        {**state, **current()},
                        code=call.reason_code or "PROVIDER_ERROR",
                        message="The provider call failed within the configured budget.",
                        call=call,
                        fake_provider=fake_provider,
                        provider_name=provider_name,
                        provider_model=provider_model,
                    )

                response: ProviderResponse = call.response

                if tokens > limits.max_total_tokens:
                    return _fallback_state(
                        {**state, **current()},
                        code="TOKEN_BUDGET_EXCEEDED",
                        message="Reported or reserved token usage exceeded the whole-turn budget.",
                        call=call,
                        fake_provider=fake_provider,
                        provider_name=provider_name,
                        provider_model=provider_model,
                    )

                if response.tool_calls:
                    if executed_tools + len(response.tool_calls) > limits.max_tool_calls:
                        return _fallback_state(
                            {**state, **current()},
                            code="TOOL_LIMIT",
                            message="The cumulative whole-turn tool budget would be exceeded.",
                            call=call,
                            fake_provider=fake_provider,
                            provider_name=provider_name,
                            provider_model=provider_model,
                        )

                    assistant_tool_calls: list[dict[str, Any]] = []
                    tool_messages: list[dict[str, Any]] = []
                    for index, tool_call in enumerate(response.tool_calls):
                        call_id = tool_call.id or f"call-{executed_tools + index + 1}"
                        with tracer.span(
                            f"tool:{tool_call.name}",
                            "tool",
                            tool=tool_call.name,
                            tool_call_id=call_id,
                            read_only=True,
                        ) as tool_span:
                            try:
                                tool_result = execute_tool(
                                    evaluation,
                                    tool_call.name,
                                    tool_call.arguments,
                                )
                                tool_span.attributes["result_summary"] = _safe_tool_summary(
                                    tool_call.name, tool_result
                                )
                            except ToolError as exc:
                                tool_span.status = "error"
                                tool_span.attributes["tool_error"] = exc.code
                                return _fallback_state(
                                    {**state, **current()},
                                    code=exc.code,
                                    message=str(exc),
                                    call=call,
                                    fake_provider=fake_provider,
                                    provider_name=provider_name,
                                    provider_model=provider_model,
                                )
                        executed_tools += 1
                        assistant_tool_calls.append(
                            {
                                "id": call_id,
                                "name": tool_call.name,
                                "arguments": tool_call.arguments,
                            }
                        )
                        tool_messages.append(
                            {
                                "role": "tool",
                                "content": json.dumps(
                                    {
                                        "tool": tool_call.name,
                                        "result": tool_result,
                                    },
                                    sort_keys=True,
                                ),
                                "tool_call_id": call_id,
                                "name": tool_call.name,
                            }
                        )

                    messages.append(
                        {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": assistant_tool_calls,
                        }
                    )
                    messages.extend(tool_messages)
                    continue

                with tracer.span(
                    "validation",
                    "validation",
                    validation_kind="schema",
                ) as validation_span:
                    try:
                        explanation = Explanation.model_validate_json(response.content)
                    except ValidationError as exc:
                        validation_span.status = "error"
                        validation_span.attributes["error_code"] = "INVALID_MODEL_OUTPUT"
                        validation_span.attributes["error_count"] = len(exc.errors())
                        if repairs < limits.max_repairs:
                            repairs += 1
                            messages.append(
                                {
                                    "role": "user",
                                    "content": (
                                        "Your previous output was not valid JSON for the "
                                        f"required schema: {exc.errors()}. Repair it once."
                                    ),
                                }
                            )
                            continue
                        return _fallback_state(
                        {**state, **current()},
                        code="INVALID_MODEL_OUTPUT",
                        message="The provider did not return a valid explanation.",
                        call=call,
                        fake_provider=fake_provider,
                        provider_name=provider_name,
                        provider_model=provider_model,
                    )

                return {
                    **current(),
                    "status": "assistant_ready",
                    "messages": messages,
                    "repairs": repairs,
                    "explanation": explanation.model_dump(mode="json"),
                    "provider_outcome": _provider_outcome(
                        call,
                        fake_provider=fake_provider,
                        transport_attempts=transport_attempts,
                        logical_attempts=calls,
                        reserved_tokens=reserved_tokens,
                        workflow_input_tokens=workflow_input_tokens,
                        workflow_output_tokens=workflow_output_tokens,
                        usage_source=(
                            "reported"
                            if usage_sources
                            and all(source == "reported" for source in usage_sources)
                            else "reserved"
                            if usage_sources
                            and all(source == "reserved" for source in usage_sources)
                            else "mixed"
                            if usage_sources
                            else "unknown"
                        ),
                        transport_attempt_usage=attempt_usage,
                    )
                    .model_copy(update={"usage_reported": usage_reported})
                    .model_dump(mode="json"),
                }

            return _fallback_state(
                {**state, **current()},
                code="EXECUTION_LIMIT",
                message="The workflow exhausted its step budget.",
                fake_provider=fake_provider,
                provider_name=provider_name,
                provider_model=provider_model,
            )

    def validate(state: WorkflowState) -> dict[str, Any]:
        evaluation = RecoveryEvaluation.model_validate(state["evaluation"])
        explanation = Explanation.model_validate(state["explanation"])
        errors = validate_explanation(explanation, evaluation)
        with tracer.span(
            "validation",
            "validation",
            error_count=len(errors),
            errors=errors,
        ) as validation_span:
            if errors:
                validation_span.status = "error"
                validation_span.attributes["error_code"] = "INVALID_EXPLANATION"

        if errors:
            repairs = int(state.get("repairs", 0))
            if repairs < limits.max_repairs:
                return {
                    "status": "repair",
                    "repairs": repairs + 1,
                    "repair_errors": errors,
                }
            return _fallback_state(
                state,
                code="INVALID_EXPLANATION",
                message="; ".join(errors),
                fake_provider=fake_provider,
                provider_name=provider_name,
                provider_model=provider_model,
            )

        safe = explanation.model_copy(
            update={
                "summary": render_explanation(
                    evaluation, explanation.recommended_candidate_id
                )
            }
        )
        return {
            "status": "completed",
            "explanation": safe.model_dump(mode="json"),
        }

    def fallback(state: WorkflowState) -> dict[str, Any]:
        evaluation = RecoveryEvaluation.model_validate(state["evaluation"])
        explanation = deterministic_explanation(evaluation)
        reason = state.get("fallback_reason") or "DETERMINISTIC_FALLBACK"
        with tracer.span("fallback", "fallback", reason=reason):
            pass
        last = state.get("last_call") or {}
        prior = state.get("provider_outcome") or {}
        attempt_usage_raw = prior.get(
            "transport_attempt_usage",
            state.get("transport_attempt_usage", []),
        )
        attempt_usage = [
            TransportAttemptUsage.model_validate(item)
            for item in (attempt_usage_raw or [])
        ]
        (
            measured_input,
            measured_output,
            reserved_input,
            reserved_output,
        ) = _attempt_token_sums(attempt_usage)
        outcome = ProviderOutcome(
            mode="deterministic_fallback",
            reason_code=reason,
            logical_attempts=int(
                prior.get("logical_attempts", last.get("logical_attempts", 0))
            ),
            transport_attempts=int(
                prior.get("transport_attempts", last.get("transport_attempts", 0))
            ),
            failed_transport_attempts=sum(1 for item in attempt_usage if not item.ok),
            reported_transport_attempts=sum(
                1 for item in attempt_usage if item.usage_source == "reported"
            ),
            reserved_transport_attempts=sum(
                1 for item in attempt_usage if item.usage_source == "reserved"
            ),
            elapsed_ms=int(prior.get("elapsed_ms", last.get("elapsed_ms", 0))),
            provider=str(prior.get("provider", last.get("provider", provider_name))),
            model=str(prior.get("model", last.get("model", provider_model))),
            input_tokens=int(prior.get("input_tokens", 0)),
            output_tokens=int(prior.get("output_tokens", 0)),
            workflow_input_tokens=int(
                prior.get(
                    "workflow_input_tokens", state.get("workflow_input_tokens", 0)
                )
            ),
            workflow_output_tokens=int(
                prior.get(
                    "workflow_output_tokens", state.get("workflow_output_tokens", 0)
                )
            ),
            measured_input_tokens=measured_input,
            measured_output_tokens=measured_output,
            reserved_input_tokens=reserved_input,
            reserved_output_tokens=reserved_output,
            reserved_tokens=int(
                prior.get("reserved_tokens", state.get("reserved_tokens", 0))
            ),
            usage_reported=bool(
                prior.get("usage_reported", state.get("usage_reported", False))
            ),
            usage_source=str(prior.get("usage_source", "unknown")),
            transport_attempt_usage=attempt_usage,
            fake_provider=fake_provider,
        )
        return {
            "status": "degraded",
            "explanation": explanation.model_dump(mode="json"),
            "provider_outcome": outcome.model_dump(mode="json"),
        }

    def approval(state: WorkflowState) -> dict[str, Any]:
        evaluation = RecoveryEvaluation.model_validate(state["evaluation"])
        explanation = Explanation.model_validate(state["explanation"])
        candidate = next(
            (
                item
                for item in evaluation.candidates
                if item.candidate_id == explanation.recommended_candidate_id
            ),
            None,
        )
        if candidate is None:
            return {"status": "completed"}
        draft = DraftRecoveryRequest.model_validate(state.get("draft") or {})
        pending = ApprovalRequest(
            run_id=tracer.run_id,
            candidate_id=candidate.candidate_id,
            project_version=draft.version,
            input_hash=candidate.base_hash,
            catalogue_hash=candidate.catalogue_hash,
            finish_offset_workdays=candidate.finish_offset_workdays,
            cost_pence=candidate.cost_pence,
        )
        with tracer.span("approval", "chain", candidate_id=candidate.candidate_id):
            decision = (
                decision_resolver(tracer.run_id)
                if decision_resolver is not None
                else None
            )
            if decision is None:
                interrupt(pending.model_dump(mode="json"))
                decision = (
                    decision_resolver(tracer.run_id)
                    if decision_resolver is not None
                    else None
                )
                if decision is None:
                    return {
                        "status": "awaiting_approval",
                        "pending_decision": pending.model_dump(mode="json"),
                    }
        approved = bool(decision.get("approved"))
        decision_id = str(decision.get("decision_id"))
        if not approved:
            return {
                "status": "rejected",
                "decision_id": decision_id,
                "pending_decision": pending.model_dump(mode="json"),
            }
        return {
            "status": (
                "degraded" if state.get("status") == "degraded" else "completed"
            ),
            "decision_id": decision_id,
            "pending_decision": pending.model_dump(mode="json"),
        }

    def finalize(state: WorkflowState) -> dict[str, Any]:
        with tracer.span("finalize", "node"):
            return {"status": state.get("status", "completed")}

    graph = StateGraph(WorkflowState)
    graph.add_node("intake", intake)
    graph.add_node("compute", compute)
    graph.add_node("assistant", assistant)
    graph.add_node("validate", validate)
    graph.add_node("fallback", fallback)
    graph.add_node("approval", approval)
    graph.add_node("finalize", finalize)

    graph.add_edge(START, "intake")
    graph.add_conditional_edges(
        "intake",
        lambda state: "finalize" if state.get("status") == "failed" else "compute",
        {"finalize": "finalize", "compute": "compute"},
    )
    graph.add_edge("compute", "assistant")
    graph.add_conditional_edges(
        "assistant",
        lambda state: "validate" if state.get("status") == "assistant_ready" else "fallback",
        {"validate": "validate", "fallback": "fallback"},
    )
    def after_validate(state: WorkflowState) -> str:
        if state.get("status") == "completed":
            explanation = state.get("explanation") or {}
            if require_approval and explanation.get("recommended_candidate_id"):
                return "approval"
            return "finalize"
        if state.get("status") == "repair":
            return "assistant"
        return "fallback"

    def after_fallback(state: WorkflowState) -> str:
        explanation = state.get("explanation") or {}
        if require_approval and explanation.get("recommended_candidate_id"):
            return "approval"
        return "finalize"

    graph.add_conditional_edges(
        "validate",
        after_validate,
        {
            "approval": "approval",
            "finalize": "finalize",
            "assistant": "assistant",
            "fallback": "fallback",
        },
    )
    graph.add_conditional_edges(
        "fallback",
        after_fallback,
        {"approval": "approval", "finalize": "finalize"},
    )
    graph.add_edge("approval", "finalize")
    graph.add_edge("finalize", END)
    return graph
