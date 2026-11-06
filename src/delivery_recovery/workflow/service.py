"""Service wrapper for the Phase 2 workflow.

The graph is process-local and uses an in-memory checkpointer for clarification
resume. That is deliberately not a durable approval store; persistent
checkpoints, jobs and decisions belong to Phase 3.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Callable

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field

from ..contracts import RecoveryEvaluation
from .contracts import (
    ApprovalRequest,
    ClarificationRequest,
    DraftRecoveryRequest,
    Explanation,
    ProviderOutcome,
    WorkflowError,
    WorkflowResult,
)
from .exporters import LangSmithTraceExporter
from .graph import build_workflow
from .providers import FakeProvider, ProviderLimits, ProviderRunner
from .tracing import TraceRecorder

__all__ = ["RecoveryWorkflow", "WorkflowLimits"]


class WorkflowLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_steps: int = Field(default=4, ge=1, le=16)
    max_provider_calls: int = Field(default=4, ge=1, le=6)
    max_tool_calls: int = Field(default=3, ge=1, le=8)
    max_repairs: int = Field(default=1, ge=0, le=2)
    max_total_tokens: int = Field(default=12_000, ge=1)
    max_output_tokens: int = Field(default=1_000, ge=1)
    max_elapsed_seconds: float = Field(default=5.0, gt=0)


class RecoveryWorkflow:
    def __init__(
        self,
        provider,
        *,
        limits: WorkflowLimits | None = None,
        clock: Callable[[], float] | None = None,
        checkpointer=None,
        source_revision: str = "unknown",
        fixture_hash: str | None = None,
        prompt_version: str = "p2-v1",
        schema_version: str = "explanation-v1",
        trace_exporter: LangSmithTraceExporter | None = None,
        trace_metadata: dict[str, Any] | None = None,
        require_approval: bool = False,
        decision_resolver: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        self.provider = provider
        self.limits = limits or WorkflowLimits()
        self.clock = clock or time.monotonic
        self.checkpointer = checkpointer or MemorySaver()
        self.trace_exporter = trace_exporter
        self.trace_metadata = trace_metadata or {}
        self.source_revision = source_revision
        self.fixture_hash = fixture_hash
        self.prompt_version = prompt_version
        self.schema_version = schema_version
        self.require_approval = require_approval
        self.decision_resolver = decision_resolver
        self.runner = ProviderRunner(
            provider,
            limits=ProviderLimits(
                max_transport_calls=2,
                max_elapsed_seconds=self.limits.max_elapsed_seconds,
            ),
            clock=self.clock,
        )
        self._fake_provider = bool(
            getattr(provider, "fake_provider", isinstance(provider, FakeProvider))
        )

    def start(
        self,
        draft: DraftRecoveryRequest,
        *,
        run_id: str | None = None,
        thread_id: str | None = None,
    ) -> WorkflowResult:
        run_id = run_id or uuid.uuid4().hex
        thread_id = thread_id or run_id
        fixture_hash = self.fixture_hash or _hash_draft(draft)
        tracer = self._tracer(run_id, thread_id, fixture_hash)
        graph = build_workflow(
            self.runner,
            tracer,
            limits=self.limits,
            fake_provider=self._fake_provider,
            provider_name=self.provider.name,
            provider_model=self.provider.model,
            clock=self.clock,
            require_approval=self.require_approval,
            decision_resolver=self.decision_resolver,
        ).compile(checkpointer=self.checkpointer)

        with tracer.span(
            "workflow",
            "chain",
            source_revision=self.source_revision,
        ):
            raw = graph.invoke(
                {
                    "draft": draft.model_dump(mode="json"),
                    "status": "new",
                    "steps": 0,
                    "provider_calls": 0,
                    "transport_attempts": 0,
                    "tool_calls": 0,
                    "total_tokens": 0,
                    "workflow_input_tokens": 0,
                    "workflow_output_tokens": 0,
                    "reserved_tokens": 0,
                    "usage_reported": False,
                    "usage_sources": [],
                    "transport_attempt_usage": [],
                    "repairs": 0,
                    "errors": [],
                    "messages": [],
                },
                config={"configurable": {"thread_id": thread_id}},
            )
        return self._finish_result(raw, run_id, thread_id, tracer)

    def resume(
        self,
        thread_id: str,
        answer: object,
        *,
        run_id: str | None = None,
    ) -> WorkflowResult:
        run_id = run_id or thread_id
        tracer = self._tracer(run_id, thread_id, self.fixture_hash or "resume")
        graph = build_workflow(
            self.runner,
            tracer,
            limits=self.limits,
            fake_provider=self._fake_provider,
            provider_name=self.provider.name,
            provider_model=self.provider.model,
            clock=self.clock,
            require_approval=self.require_approval,
            decision_resolver=self.decision_resolver,
        ).compile(checkpointer=self.checkpointer)
        with tracer.span(
            "workflow",
            "chain",
            source_revision=self.source_revision,
        ):
            raw = graph.invoke(
                Command(resume=answer),
                config={"configurable": {"thread_id": thread_id}},
            )
        return self._finish_result(raw, run_id, thread_id, tracer)

    def _finish_result(
        self,
        raw: dict[str, Any],
        run_id: str,
        thread_id: str,
        tracer: TraceRecorder,
    ) -> WorkflowResult:
        result = self._to_result(raw, run_id, thread_id, tracer)
        tracer.finalize(
            result.status,
            reason_code=(
                result.provider_outcome.reason_code
                if result.provider_outcome is not None
                else None
            ),
            safe_summary=_safe_summary(result),
        )
        result = result.model_copy(update={"trace": tracer.export()})
        if self.trace_exporter is not None and result.trace is not None:
            result = result.model_copy(
                update={"trace_export": self.trace_exporter.export(result.trace)}
            )
        return result

    def _tracer(self, run_id: str, thread_id: str, fixture_hash: str) -> TraceRecorder:
        return TraceRecorder(
            run_id,
            metadata={
                "thread_id": thread_id,
                "source_revision": self.source_revision,
                "fixture_hash": fixture_hash,
                "prompt_version": self.prompt_version,
                "schema_version": self.schema_version,
                "provider": self.provider.name,
                "model": self.provider.model,
                "fake_provider": self._fake_provider,
                **self.trace_metadata,
            },
        )

    def _to_result(
        self,
        raw: dict[str, Any],
        run_id: str,
        thread_id: str,
        tracer: TraceRecorder,
    ) -> WorkflowResult:
        trace = tracer.export()
        interrupts = raw.get("__interrupt__")
        if interrupts:
            value = interrupts[0].value
            if isinstance(value, dict) and value.get("kind") == "approval":
                return WorkflowResult(
                    run_id=run_id,
                    thread_id=thread_id,
                    status="awaiting_approval",
                    evaluation=(
                        RecoveryEvaluation.model_validate(raw["evaluation"])
                        if raw.get("evaluation")
                        else None
                    ),
                    explanation=(
                        Explanation.model_validate(raw["explanation"])
                        if raw.get("explanation")
                        else None
                    ),
                    provider_outcome=(
                        ProviderOutcome.model_validate(raw["provider_outcome"])
                        if raw.get("provider_outcome")
                        else None
                    ),
                    pending_decision=ApprovalRequest.model_validate(value),
                    trace=trace,
                )
            return WorkflowResult(
                run_id=run_id,
                thread_id=thread_id,
                status="needs_input",
                clarification=ClarificationRequest.model_validate(value),
                trace=trace,
            )

        status = raw.get("status", "failed")
        if status not in {
            "completed",
            "degraded",
            "rejected",
            "failed",
        }:
            status = "failed"

        evaluation = (
            RecoveryEvaluation.model_validate(raw["evaluation"])
            if raw.get("evaluation")
            else None
        )
        explanation = (
            Explanation.model_validate(raw["explanation"])
            if raw.get("explanation")
            else None
        )
        provider_outcome = (
            ProviderOutcome.model_validate(raw["provider_outcome"])
            if raw.get("provider_outcome")
            else None
        )
        errors = [WorkflowError.model_validate(item) for item in raw.get("errors") or []]

        return WorkflowResult(
            run_id=run_id,
            thread_id=thread_id,
            status=status,  # type: ignore[arg-type]
            evaluation=evaluation,
            explanation=explanation,
            provider_outcome=provider_outcome,
            pending_decision=(
                ApprovalRequest.model_validate(raw["pending_decision"])
                if raw.get("pending_decision")
                else None
            ),
            decision_id=raw.get("decision_id"),
            errors=errors,
            trace=trace,
        )


def _safe_summary(result: WorkflowResult) -> dict[str, Any]:
    """Allowlisted result content for trace export; no prompts or credentials."""

    evaluation = result.evaluation
    outcome = result.provider_outcome
    explanation = result.explanation
    tool_summaries = []
    for span in (result.trace or {}).get("spans", []):
        if span.get("kind") == "tool":
            tool_summaries.append(
                {
                    "tool": span.get("attributes", {}).get("tool"),
                    "result_summary": span.get("attributes", {}).get("result_summary"),
                }
            )
    return {
        "status": result.status,
        "reason_code": outcome.reason_code if outcome else None,
        "recommendation": explanation.recommended_candidate_id if explanation else None,
        "baseline_finish": evaluation.baseline.finish_offset_workdays if evaluation else None,
        "disrupted_finish": evaluation.disrupted.finish_offset_workdays if evaluation else None,
        "recovered_finish": explanation.finish_offset_workdays if explanation else None,
        "cost_pence": explanation.cost_pence if explanation else None,
        "approval_state": "not_recorded",
        "provider": outcome.provider if outcome else None,
        "model": outcome.model if outcome else None,
        "tools": tool_summaries,
        "usage": {
            "call_input_tokens": outcome.input_tokens if outcome else None,
            "call_output_tokens": outcome.output_tokens if outcome else None,
            "workflow_input_tokens": outcome.workflow_input_tokens if outcome else None,
            "workflow_output_tokens": outcome.workflow_output_tokens if outcome else None,
            "measured_input_tokens": outcome.measured_input_tokens if outcome else None,
            "measured_output_tokens": outcome.measured_output_tokens if outcome else None,
            "reserved_input_tokens": outcome.reserved_input_tokens if outcome else None,
            "reserved_output_tokens": outcome.reserved_output_tokens if outcome else None,
            "reserved_tokens": outcome.reserved_tokens if outcome else None,
            "transport_attempts": outcome.transport_attempts if outcome else None,
            "failed_transport_attempts": (
                outcome.failed_transport_attempts if outcome else None
            ),
            "reported_transport_attempts": (
                outcome.reported_transport_attempts if outcome else None
            ),
            "reserved_transport_attempts": (
                outcome.reserved_transport_attempts if outcome else None
            ),
            "attempt_usage": (
                [
                    item.model_dump(mode="json")
                    for item in outcome.transport_attempt_usage
                ]
                if outcome
                else []
            ),
            "usage_source": outcome.usage_source if outcome else "unknown",
            "measured_billing": "unavailable",
            "estimated_cost_usd": None,
        },
    }


def _hash_draft(draft: DraftRecoveryRequest) -> str:
    canonical = json.dumps(
        draft.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
