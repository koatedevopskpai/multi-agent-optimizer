"""Typed contracts for the Phase 2 recovery workflow."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..contracts import Disruption, ProjectInput, RecoveryEvaluation, RecoveryOption, TaskInput

__all__ = [
    "ApprovalRequest",
    "ClarificationRequest",
    "DraftRecoveryRequest",
    "Explanation",
    "MissingFact",
    "ProviderOutcome",
    "TransportAttemptUsage",
    "WorkflowError",
    "WorkflowResult",
]

StrictNonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
StrictPositiveInt = Annotated[int, Field(strict=True, ge=1)]


class MissingFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    question: str


class DraftRecoveryRequest(BaseModel):
    """Permissive intake that can represent missing business facts.

    Supplied values remain typed. Missing fields become targeted clarification
    questions rather than invented defaults.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["delivery-recovery-v1"] = "delivery-recovery-v1"
    project_id: str = "DRAFT-PROJECT"
    version: StrictPositiveInt = 1
    time_unit: Literal["working_day"] | None = None
    start_offset_workdays: StrictNonNegativeInt = 0
    target_finish_offset_workdays: StrictNonNegativeInt | None = None
    budget_pence: StrictNonNegativeInt | None = None
    currency: Literal["GBP"] = "GBP"
    tasks: list[TaskInput] | None = None
    catalogue: list[RecoveryOption] = Field(default_factory=list)
    disruption: Disruption | None = None

    def missing_facts(self) -> list[MissingFact]:
        missing: list[MissingFact] = []
        if self.time_unit is None:
            missing.append(
                MissingFact(
                    field="time_unit",
                    question="What time unit should the schedule use? (working_day)",
                )
            )
        if self.target_finish_offset_workdays is None:
            missing.append(
                MissingFact(
                    field="target_finish_offset_workdays",
                    question="What target finish offset should the recovery meet?",
                )
            )
        if self.budget_pence is None:
            missing.append(
                MissingFact(
                    field="budget_pence",
                    question="What recovery budget is available, in GBP pence?",
                )
            )
        if not self.tasks:
            missing.append(
                MissingFact(
                    field="tasks",
                    question="Please provide the project tasks and their dependencies.",
                )
            )
        return missing

    def with_answer(self, field: str, value: object) -> "DraftRecoveryRequest":
        data = self.model_dump(mode="json")
        data[field] = value
        return DraftRecoveryRequest.model_validate(data)

    def to_project_input(self) -> ProjectInput:
        missing = self.missing_facts()
        if missing:
            raise ValueError(f"draft is missing required facts: {[item.field for item in missing]}")
        return ProjectInput(
            schema_version=self.schema_version,
            project_id=self.project_id,
            version=self.version,
            time_unit=self.time_unit,
            start_offset_workdays=self.start_offset_workdays,
            target_finish_offset_workdays=self.target_finish_offset_workdays,
            budget_pence=self.budget_pence,
            currency=self.currency,
            tasks=self.tasks,
            catalogue=self.catalogue,
        )


class Explanation(BaseModel):
    """Constrained model explanation, checked against deterministic facts."""

    model_config = ConfigDict(extra="forbid")

    recommended_candidate_id: str | None
    summary: str = Field(min_length=1)
    finish_offset_workdays: int | None = None
    cost_pence: int | None = None

    @model_validator(mode="after")
    def _candidate_fields(self) -> "Explanation":
        if self.recommended_candidate_id is None:
            if self.finish_offset_workdays is not None or self.cost_pence is not None:
                raise ValueError("a no-candidate explanation must not invent finish or cost facts")
        elif self.finish_offset_workdays is None or self.cost_pence is None:
            raise ValueError("a candidate explanation must include finish and cost")
        return self


class ClarificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    question: str


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["approval"] = "approval"
    run_id: str
    candidate_id: str
    project_version: int
    input_hash: str
    catalogue_hash: str
    finish_offset_workdays: int | None = None
    cost_pence: int | None = None


class TransportAttemptUsage(BaseModel):
    """Safe per-attempt accounting for one actual provider transport."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    call: int = Field(ge=1)
    attempt: int = Field(ge=1)
    ok: bool
    usage_source: Literal["reported", "reserved"]
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    reservation_tokens: int = Field(ge=0)


class ProviderOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["model", "deterministic_fallback"]
    reason_code: str | None = None
    logical_attempts: int = 0
    transport_attempts: int = 0
    failed_transport_attempts: int = 0
    reported_transport_attempts: int = 0
    reserved_transport_attempts: int = 0
    elapsed_ms: int = 0
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    workflow_input_tokens: int = 0
    workflow_output_tokens: int = 0
    measured_input_tokens: int = 0
    measured_output_tokens: int = 0
    reserved_input_tokens: int = 0
    reserved_output_tokens: int = 0
    reserved_tokens: int = 0
    usage_reported: bool = False
    usage_source: str = "unknown"
    transport_attempt_usage: list[TransportAttemptUsage] = Field(default_factory=list)
    fake_provider: bool = False


class WorkflowError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str


class WorkflowResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    thread_id: str
    status: Literal[
        "completed",
        "degraded",
        "needs_input",
        "awaiting_approval",
        "rejected",
        "failed",
    ]
    evaluation: RecoveryEvaluation | None = None
    explanation: Explanation | None = None
    provider_outcome: ProviderOutcome | None = None
    clarification: ClarificationRequest | None = None
    pending_decision: ApprovalRequest | None = None
    decision_id: str | None = None
    errors: list[WorkflowError] = Field(default_factory=list)
    trace: dict | None = None
    trace_export: dict | None = None
