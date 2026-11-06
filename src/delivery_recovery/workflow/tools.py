"""Allowlisted read-only tools over a verified recovery evaluation.

There is deliberately no tool that mutates the project, catalogue, costs,
dependencies, permissions or approval state. Tool arguments and referenced
candidate IDs are validated before any fact is returned.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..contracts import RecoveryEvaluation

__all__ = ["TOOL_SPECS", "ToolError", "execute_tool"]


class ToolError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ScheduleFactsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: Literal["baseline", "disrupted"]


class CompareCandidatesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_ids: list[str] = Field(min_length=1)


class ExplainConstraintArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    arguments_model: type[BaseModel]
    read_only: bool = True


TOOL_SPECS: dict[str, ToolSpec] = {
    "get_schedule_facts": ToolSpec(
        name="get_schedule_facts",
        description="Return verified baseline or disrupted CPM facts.",
        arguments_model=ScheduleFactsArgs,
    ),
    "compare_candidates": ToolSpec(
        name="compare_candidates",
        description="Return verified facts for explicitly requested candidate IDs.",
        arguments_model=CompareCandidatesArgs,
    ),
    "explain_constraint": ToolSpec(
        name="explain_constraint",
        description="Explain why a specific candidate is feasible or rejected.",
        arguments_model=ExplainConstraintArgs,
    ),
}


def _validated_arguments(spec: ToolSpec, arguments: dict[str, Any]) -> BaseModel:
    try:
        return spec.arguments_model.model_validate(arguments)
    except ValidationError as exc:
        raise ToolError("INVALID_TOOL_ARGUMENTS", str(exc)) from exc


def _candidate(evaluation: RecoveryEvaluation, candidate_id: str):
    for candidate in evaluation.candidates:
        if candidate.candidate_id == candidate_id:
            return candidate
    raise ToolError("UNKNOWN_CANDIDATE", f"unknown candidate ID {candidate_id!r}")


def execute_tool(
    evaluation: RecoveryEvaluation,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Execute one allowlisted read-only tool.

    Raises ``ToolError`` for an unregistered tool, invalid arguments or an
    unknown candidate. The evaluation is never mutated.
    """

    spec = TOOL_SPECS.get(name)
    if spec is None:
        raise ToolError("TOOL_NOT_ALLOWED", f"tool {name!r} is not allowlisted")

    args = _validated_arguments(spec, arguments)

    if name == "get_schedule_facts":
        phase = args.phase  # type: ignore[attr-defined]
        facts = evaluation.baseline if phase == "baseline" else evaluation.disrupted
        return {
            **facts.model_dump(mode="json"),
            "phase": phase,
            "source": "deterministic_cpm",
        }

    if name == "compare_candidates":
        candidate_ids = args.candidate_ids  # type: ignore[attr-defined]
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ToolError("INVALID_TOOL_ARGUMENTS", "candidate_ids must be unique")
        return {
            "candidates": [
                _candidate(evaluation, candidate_id).model_dump(mode="json")
                for candidate_id in candidate_ids
            ],
            "source": "deterministic_cpm",
        }

    if name == "explain_constraint":
        candidate = _candidate(evaluation, args.candidate_id)  # type: ignore[attr-defined]
        return {
            "candidate_id": candidate.candidate_id,
            "feasible": candidate.feasible,
            "rejection_codes": list(candidate.rejection_codes),
            "finish_offset_workdays": candidate.finish_offset_workdays,
            "cost_pence": candidate.cost_pence,
            "project_saving_workdays": candidate.project_saving_workdays,
            "source": "deterministic_cpm",
        }

    raise ToolError("TOOL_NOT_ALLOWED", f"tool {name!r} is not implemented")
