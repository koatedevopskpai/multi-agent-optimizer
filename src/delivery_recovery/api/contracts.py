"""Request contracts for the recovery API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..contracts import Disruption, ProjectInput
from ..workflow.contracts import DraftRecoveryRequest

__all__ = [
    "ClarificationSubmit",
    "RecoveryRequest",
    "RunCreateRequest",
    "RunView",
]


class RecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProjectInput
    disruption: Disruption | None = None
    option_ids: list[str] | None = None


class RunCreateRequest(BaseModel):
    """Durable run submission. The draft may still need human clarification."""

    model_config = ConfigDict(extra="forbid")

    draft: DraftRecoveryRequest
    scenario_id: str | None = None


class ClarificationSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    answer: Any


class RunView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    thread_id: str
    status: str
    project_id: str
    project_version: int
    created_by: str
    revision: int = 0
    decision_status: str | None = None
    evaluation: dict[str, Any] | None = None
    explanation: dict[str, Any] | None = None
    provider_outcome: dict[str, Any] | None = None
    pending_question: dict[str, Any] | None = None
    pending_decision: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    created_at: str
    updated_at: str
    retention_expires_at: str | None = None
    access: dict[str, Any] = Field(default_factory=dict)
