"""Boundary tests for the strict recovery contracts.

These exercise the validation branches that protect the domain from ambiguous
or malformed input before any schedule calculation or provider work.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from delivery_recovery import (
    Disruption,
    ProjectInput,
    RecoveryOption,
    TaskDurationChange,
    TaskInput,
)


def _task(task_id: str = "A", **overrides) -> TaskInput:
    values = {"id": task_id, "name": f"Task {task_id}", "duration_workdays": 2}
    values.update(overrides)
    return TaskInput(**values)


def _option(option_id: str = "OPTION", **overrides) -> RecoveryOption:
    values = {
        "option_id": option_id,
        "changes": [TaskDurationChange(task_id="A", new_duration_workdays=1)],
        "incremental_cost_pence": 100,
    }
    values.update(overrides)
    return RecoveryOption(**values)


def _project(**overrides) -> ProjectInput:
    values = {
        "project_id": "CONTRACT-DEMO",
        "target_finish_offset_workdays": 2,
        "budget_pence": 1_000,
        "tasks": [_task()],
    }
    values.update(overrides)
    return ProjectInput(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "   "},
        {"name": "   "},
        {"dependency_ids": ["A", "A"]},
        {"dependency_ids": ["   "]},
        {"duration_workdays": -1},
        {"duration_workdays": 1.5},
        {"duration_workdays": True},
    ],
)
def test_task_boundary_validation(overrides):
    with pytest.raises(ValidationError):
        _task("A", **overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"task_id": "   "},
        {"new_duration_workdays": -1},
        {"new_duration_workdays": True},
    ],
)
def test_duration_change_boundary_validation(overrides):
    values = {"task_id": "A", "new_duration_workdays": 1}
    values.update(overrides)
    with pytest.raises(ValidationError):
        TaskDurationChange(**values)


def test_option_rejects_blank_id_duplicate_task_changes_and_duplicate_incompatibilities():
    with pytest.raises(ValidationError):
        _option("   ")

    with pytest.raises(ValidationError):
        _option(
            changes=[
                TaskDurationChange(task_id="A", new_duration_workdays=1),
                TaskDurationChange(task_id="A", new_duration_workdays=0),
            ]
        )

    with pytest.raises(ValidationError):
        _option(incompatible_option_ids=["OTHER", "OTHER"])


def test_project_rejects_duplicate_tasks_and_unknown_dependencies():
    with pytest.raises(ValidationError):
        _project(tasks=[_task("A"), _task("A")])

    with pytest.raises(ValidationError):
        _project(tasks=[_task("A", dependency_ids=["Z"])])


def test_project_rejects_duplicate_catalogue_options_and_unknown_changed_tasks():
    with pytest.raises(ValidationError):
        _project(catalogue=[_option("ONE"), _option("ONE")])

    with pytest.raises(ValidationError):
        _project(
            catalogue=[
                _option(
                    changes=[
                        TaskDurationChange(task_id="Z", new_duration_workdays=1)
                    ]
                )
            ]
        )


def test_project_rejects_unknown_and_self_incompatibility():
    with pytest.raises(ValidationError):
        _project(catalogue=[_option(incompatible_option_ids=["MISSING"])])

    with pytest.raises(ValidationError):
        _project(catalogue=[_option(incompatible_option_ids=["OPTION"])])


@pytest.mark.parametrize(
    "overrides",
    [
        {"project_id": "   "},
        {"budget_pence": -1},
        {"target_finish_offset_workdays": -1},
        {"start_offset_workdays": -1},
        {"time_unit": "calendar_day"},
        {"currency": "EUR"},
        {"schema_version": "other-schema"},
        {"tasks": []},
    ],
)
def test_project_boundary_validation(overrides):
    with pytest.raises(ValidationError):
        _project(**overrides)


def test_disruption_requires_a_non_blank_task_and_non_negative_duration():
    with pytest.raises(ValidationError):
        Disruption(task_id="   ", revised_duration_workdays=1)

    with pytest.raises(ValidationError):
        Disruption(task_id="A", revised_duration_workdays=-1)


def test_project_rejects_more_than_200_dependency_edges():
    tasks = [TaskInput(id="T0", name="T0", duration_workdays=1)]
    for index in range(1, 21):
        tasks.append(
            TaskInput(
                id=f"T{index}",
                name=f"T{index}",
                duration_workdays=1,
                dependency_ids=[f"T{prior}" for prior in range(index)],
            )
        )

    with pytest.raises(ValidationError):
        _project(tasks=tasks)
