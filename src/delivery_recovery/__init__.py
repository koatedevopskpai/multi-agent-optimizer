"""Delivery Recovery Assistant domain package.

Phase 1 exposes only the deterministic contracts and candidate evaluator.
Workflow, API, UI, provider and approval layers are later slices.
"""

from .candidates import evaluate_recovery
from .contracts import (
    CandidateResult,
    Disruption,
    PolicyError,
    ProjectInput,
    RecoveryEvaluation,
    RecoveryOption,
    ScheduleFacts,
    TaskDurationChange,
    TaskInput,
    TaskTiming,
)

__all__ = [
    "CandidateResult",
    "Disruption",
    "PolicyError",
    "ProjectInput",
    "RecoveryEvaluation",
    "RecoveryOption",
    "ScheduleFacts",
    "TaskDurationChange",
    "TaskInput",
    "TaskTiming",
    "evaluate_recovery",
]
