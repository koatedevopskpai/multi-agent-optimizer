"""Typed contracts for the Delivery Recovery Assistant domain.

The contracts are deliberately strict at the application boundary. A missing
business fact belongs in a draft/intake model (a later slice); these models
accept only supplied values that the deterministic service can evaluate.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
]

StrictNonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
StrictPositiveInt = Annotated[int, Field(strict=True, ge=1)]


class PolicyError(ValueError):
    """A supplied catalogue action is not permitted by the domain policy."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    duration_workdays: StrictNonNegativeInt
    dependency_ids: list[str] = Field(default_factory=list)

    @field_validator("id", "name")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("dependency_ids")
    @classmethod
    def _unique_dependencies(cls, value: list[str]) -> list[str]:
        if any(not dependency.strip() for dependency in value):
            raise ValueError("dependency ids must not be blank")
        if len(set(value)) != len(value):
            raise ValueError("dependency ids must be unique")
        return value


class TaskDurationChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1)
    new_duration_workdays: StrictNonNegativeInt

    @field_validator("task_id")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class RecoveryOption(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    option_id: str = Field(min_length=1)
    changes: list[TaskDurationChange] = Field(min_length=1)
    incremental_cost_pence: StrictNonNegativeInt
    assumptions: list[str] = Field(default_factory=list)
    incompatible_option_ids: list[str] = Field(default_factory=list)

    @field_validator("option_id")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("changes")
    @classmethod
    def _one_change_per_task(cls, value: list[TaskDurationChange]) -> list[TaskDurationChange]:
        task_ids = [change.task_id for change in value]
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("an option may change each task at most once")
        return value

    @field_validator("incompatible_option_ids")
    @classmethod
    def _unique_incompatibilities(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("incompatible option ids must be unique")
        return value


class Disruption(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1)
    revised_duration_workdays: StrictNonNegativeInt

    @field_validator("task_id")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class ProjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["delivery-recovery-v1"] = "delivery-recovery-v1"
    project_id: str = Field(min_length=1)
    version: StrictPositiveInt = 1
    time_unit: Literal["working_day"] = "working_day"
    start_offset_workdays: StrictNonNegativeInt = 0
    target_finish_offset_workdays: StrictNonNegativeInt
    budget_pence: StrictNonNegativeInt
    currency: Literal["GBP"] = "GBP"
    tasks: list[TaskInput] = Field(min_length=1, max_length=50)
    catalogue: list[RecoveryOption] = Field(default_factory=list, max_length=4)

    @field_validator("project_id")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def _referential_integrity(self) -> "ProjectInput":
        task_ids = [task.id for task in self.tasks]
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("task ids must be unique")
        known = set(task_ids)
        edge_count = sum(len(task.dependency_ids) for task in self.tasks)
        if edge_count > 200:
            raise ValueError(f"project has {edge_count} dependency edges; maximum is 200")
        for task in self.tasks:
            unknown = sorted(set(task.dependency_ids) - known)
            if unknown:
                raise ValueError(f"task {task.id!r} depends on unknown task(s): {unknown}")

        option_ids = [option.option_id for option in self.catalogue]
        if len(set(option_ids)) != len(option_ids):
            raise ValueError("catalogue option ids must be unique")
        known_options = set(option_ids)
        for option in self.catalogue:
            for change in option.changes:
                if change.task_id not in known:
                    raise ValueError(
                        f"option {option.option_id!r} changes unknown task {change.task_id!r}"
                    )
            unknown_incompatible = sorted(set(option.incompatible_option_ids) - known_options)
            if unknown_incompatible:
                raise ValueError(
                    f"option {option.option_id!r} names unknown incompatible option(s): "
                    f"{unknown_incompatible}"
                )
            if option.option_id in option.incompatible_option_ids:
                raise ValueError(f"option {option.option_id!r} cannot be incompatible with itself")
        return self


class TaskTiming(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    early_start: int
    early_finish: int
    slack: int


class ScheduleFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_hash: str
    start_offset_workdays: int
    finish_offset_workdays: int
    elapsed_workdays: int
    critical_task_ids: list[str]
    task_timings: dict[str, TaskTiming]


class CandidateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    base_hash: str
    catalogue_hash: str
    option_ids: list[str]
    task_reductions: dict[str, int]
    project_saving_workdays: int
    finish_offset_workdays: int
    cost_pence: int
    feasible: bool
    rejection_codes: list[str]


class RecoveryEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    baseline: ScheduleFacts
    disrupted: ScheduleFacts
    candidates: list[CandidateResult]
    recommended_candidate_id: str | None
    ranked_feasible_candidate_ids: list[str]

    def candidate_for(self, option_ids: list[str]) -> CandidateResult:
        wanted = tuple(sorted(option_ids))
        for candidate in self.candidates:
            if tuple(sorted(candidate.option_ids)) == wanted:
                return candidate
        raise KeyError(f"no candidate for option ids {option_ids!r}")
