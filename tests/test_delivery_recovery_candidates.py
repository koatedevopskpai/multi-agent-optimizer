"""Business-first tests for the deterministic delivery-recovery domain.

The planning fixtures are synthetic. These tests assert the rule that matters:
a task reduction is not a project saving unless the whole network is recomputed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from delivery_recovery import (
    Disruption,
    PolicyError,
    ProjectInput,
    RecoveryOption,
    TaskDurationChange,
    TaskInput,
    evaluate_recovery,
)

FIXTURES = Path(__file__).parent / "fixtures" / "delivery_recovery"


def _utility_catalogue() -> list[RecoveryOption]:
    raw = json.loads((FIXTURES / "utility_recovery.json").read_text(encoding="utf-8"))
    return [
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
    ]


def _utility_project(*, budget_pence: int = 200_000, catalogue=None) -> ProjectInput:
    raw = json.loads((FIXTURES / "utility_recovery.json").read_text(encoding="utf-8"))
    tasks = [
        TaskInput(
            id=task_id,
            name=body["name"],
            duration_workdays=body["duration"],
            dependency_ids=body["dependencies"],
        )
        for task_id, body in raw["baseline_tasks"].items()
    ]
    options = catalogue if catalogue is not None else _utility_catalogue()
    return ProjectInput(
        project_id=raw["project_id"],
        version=raw["project_version"],
        start_offset_workdays=raw["start_offset_workdays"],
        target_finish_offset_workdays=raw["target_finish_offset_workdays"],
        budget_pence=budget_pence,
        tasks=tasks,
        catalogue=options,
    )


def _utility_disruption() -> Disruption:
    raw = json.loads((FIXTURES / "utility_recovery.json").read_text(encoding="utf-8"))
    return Disruption(
        task_id=raw["disruption"]["task_id"],
        revised_duration_workdays=raw["disruption"]["revised_duration_workdays"],
    )


def _parallel_project(*, options: list[RecoveryOption]) -> ProjectInput:
    return ProjectInput(
        project_id="PARALLEL-DEMO-001",
        version=1,
        start_offset_workdays=0,
        target_finish_offset_workdays=5,
        budget_pence=10_000,
        tasks=[
            TaskInput(id="S", name="Start", duration_workdays=1),
            TaskInput(id="P", name="Branch P", duration_workdays=3, dependency_ids=["S"]),
            TaskInput(id="Q", name="Branch Q", duration_workdays=3, dependency_ids=["S"]),
            TaskInput(id="E", name="End", duration_workdays=1, dependency_ids=["P", "Q"]),
        ],
        catalogue=options,
    )


def _shorten(task_id: str, option_id: str | None = None) -> RecoveryOption:
    return RecoveryOption(
        option_id=option_id or f"SHORTEN_{task_id}",
        changes=[TaskDurationChange(task_id=task_id, new_duration_workdays=2)],
        incremental_cost_pence=500,
    )


def test_shortening_one_parallel_critical_task_does_not_save_project_time():
    project = _parallel_project(options=[_shorten("P")])
    before = project.model_dump()

    evaluation = evaluate_recovery(
        project,
        Disruption(task_id="P", revised_duration_workdays=3),
        option_ids=["SHORTEN_P"],
    )
    candidate = evaluation.candidate_for(["SHORTEN_P"])

    assert candidate.task_reductions == {"P": 1}
    assert candidate.project_saving_workdays == 0
    assert candidate.finish_offset_workdays == 5
    assert project.model_dump() == before


def test_shortening_both_parallel_branches_saves_one_project_day():
    project = _parallel_project(options=[_shorten("P"), _shorten("Q")])
    evaluation = evaluate_recovery(
        project,
        Disruption(task_id="P", revised_duration_workdays=3),
        option_ids=["SHORTEN_P", "SHORTEN_Q"],
    )
    candidate = evaluation.candidate_for(["SHORTEN_P", "SHORTEN_Q"])

    assert candidate.task_reductions == {"P": 1, "Q": 1}
    assert candidate.project_saving_workdays == 1
    assert candidate.finish_offset_workdays == 4


def test_utility_catalogue_is_fully_recomputed_and_ranked():
    project = _utility_project()
    evaluation = evaluate_recovery(project, _utility_disruption())

    assert evaluation.baseline.finish_offset_workdays == 15
    assert evaluation.disrupted.finish_offset_workdays == 17
    assert evaluation.baseline.critical_task_ids == ["A", "C", "D", "E", "G"]

    by_options = {tuple(c.option_ids): c for c in evaluation.candidates}
    assert set(by_options) == {
        (),
        ("EXPEDITE_PANEL",),
        ("INSTALL_SUPPORT",),
        ("EXPEDITE_PANEL", "INSTALL_SUPPORT"),
    }

    none = by_options[()]
    assert (none.finish_offset_workdays, none.cost_pence, none.project_saving_workdays) == (17, 0, 0)
    assert none.feasible is False
    assert none.rejection_codes == ["TARGET_MISSED"]

    expedite = by_options[("EXPEDITE_PANEL",)]
    assert (expedite.finish_offset_workdays, expedite.cost_pence, expedite.project_saving_workdays) == (
        15,
        180_000,
        2,
    )
    assert expedite.feasible is True
    assert expedite.rejection_codes == []

    support = by_options[("INSTALL_SUPPORT",)]
    assert (support.finish_offset_workdays, support.cost_pence, support.project_saving_workdays) == (
        16,
        120_000,
        1,
    )
    assert support.feasible is False
    assert support.rejection_codes == ["TARGET_MISSED"]

    both = by_options[("EXPEDITE_PANEL", "INSTALL_SUPPORT")]
    assert (both.finish_offset_workdays, both.cost_pence, both.project_saving_workdays) == (
        14,
        300_000,
        3,
    )
    assert both.feasible is False
    assert both.rejection_codes == ["BUDGET_EXCEEDED"]

    expedite_id = expedite.candidate_id
    assert evaluation.recommended_candidate_id == expedite_id
    assert evaluation.ranked_feasible_candidate_ids == [expedite_id]


def test_budget_equality_is_eligible_and_one_penny_below_is_not():
    disruption = _utility_disruption()

    at_budget = evaluate_recovery(_utility_project(budget_pence=180_000), disruption)
    expedite_at = at_budget.candidate_for(["EXPEDITE_PANEL"])
    assert expedite_at.feasible is True
    assert expedite_at.rejection_codes == []
    assert at_budget.recommended_candidate_id == expedite_at.candidate_id

    below_budget = evaluate_recovery(_utility_project(budget_pence=179_999), disruption)
    expedite_below = below_budget.candidate_for(["EXPEDITE_PANEL"])
    assert expedite_below.feasible is False
    assert "BUDGET_EXCEEDED" in expedite_below.rejection_codes
    assert below_budget.recommended_candidate_id is None


def test_no_feasible_candidate_is_reported_without_relaxing_constraints():
    evaluation = evaluate_recovery(_utility_project(budget_pence=100_000), _utility_disruption())

    assert evaluation.recommended_candidate_id is None
    assert evaluation.ranked_feasible_candidate_ids == []
    assert all(candidate.feasible is False for candidate in evaluation.candidates)
    assert all(candidate.rejection_codes for candidate in evaluation.candidates)


def test_unknown_or_non_reducing_catalogue_change_is_rejected_policy():
    project = _utility_project()
    disruption = _utility_disruption()

    with pytest.raises(PolicyError) as unknown:
        evaluate_recovery(project, disruption, option_ids=["SHORTEN_COMMISSIONING"])
    assert unknown.value.code == "UNKNOWN_OPTION"

    extend = RecoveryOption(
        option_id="EXTEND_PANEL",
        changes=[TaskDurationChange(task_id="C", new_duration_workdays=8)],
        incremental_cost_pence=0,
    )
    with_extend = project.model_copy(update={"catalogue": [*project.catalogue, extend]})
    with pytest.raises(PolicyError) as non_reduction:
        evaluate_recovery(with_extend, disruption, option_ids=["EXTEND_PANEL"])
    assert non_reduction.value.code == "NON_REDUCTION"


def test_nonzero_start_separates_finish_offset_from_elapsed_duration():
    project = ProjectInput(
        project_id="OFFSET-DEMO-001",
        version=1,
        start_offset_workdays=10,
        target_finish_offset_workdays=21,
        budget_pence=0,
        tasks=[
            TaskInput(id="A", name="Start", duration_workdays=3),
            TaskInput(id="B", name="Finish", duration_workdays=8, dependency_ids=["A"]),
        ],
    )
    evaluation = evaluate_recovery(project, None)

    assert evaluation.baseline.finish_offset_workdays == 21
    assert evaluation.baseline.elapsed_workdays == 11
    assert evaluation.baseline.critical_task_ids == ["A", "B"]


def test_contract_rejects_duplicate_ids_bool_durations_and_unknown_dependencies():
    with pytest.raises(ValidationError):
        TaskInput(id="A", name="A", duration_workdays=True)

    project = _utility_project()
    duplicate = project.model_copy(update={"tasks": [*project.tasks, project.tasks[0]]})
    with pytest.raises(ValidationError):
        ProjectInput.model_validate(duplicate.model_dump())

    unknown_dependency = project.model_copy(
        update={
            "tasks": [
                TaskInput(id="A", name="A", duration_workdays=1, dependency_ids=["Z"]),
            ]
        }
    )
    with pytest.raises(ValidationError):
        ProjectInput.model_validate(unknown_dependency.model_dump())


def _incompatible_utility_catalogue(*, reverse: bool) -> list[RecoveryOption]:
    expedite, support = _utility_catalogue()
    if reverse:
        support = support.model_copy(
            update={"incompatible_option_ids": ["EXPEDITE_PANEL"]}
        )
    else:
        expedite = expedite.model_copy(
            update={"incompatible_option_ids": ["INSTALL_SUPPORT"]}
        )
    return [expedite, support]


@pytest.mark.parametrize("reverse", [False, True])
def test_incompatible_options_are_excluded_from_enumeration_in_either_direction(reverse):
    project = _utility_project(
        budget_pence=400_000,
        catalogue=_incompatible_utility_catalogue(reverse=reverse),
    ).model_copy(update={"target_finish_offset_workdays": 16})

    evaluation = evaluate_recovery(project, _utility_disruption())
    selections = {tuple(sorted(candidate.option_ids)) for candidate in evaluation.candidates}

    assert ("EXPEDITE_PANEL", "INSTALL_SUPPORT") not in selections
    assert ("EXPEDITE_PANEL",) in selections
    assert ("INSTALL_SUPPORT",) in selections
    support = evaluation.candidate_for(["INSTALL_SUPPORT"])
    assert evaluation.recommended_candidate_id == support.candidate_id

    for option_ids in (
        ["EXPEDITE_PANEL", "INSTALL_SUPPORT"],
        ["INSTALL_SUPPORT", "EXPEDITE_PANEL"],
    ):
        with pytest.raises(PolicyError) as exc:
            evaluate_recovery(project, _utility_disruption(), option_ids=option_ids)
        assert exc.value.code == "INCOMPATIBLE_OPTIONS"


def test_forbidden_pair_is_never_recommended_even_when_it_would_meet_constraints():
    project = _utility_project(
        budget_pence=400_000,
        catalogue=_incompatible_utility_catalogue(reverse=False),
    ).model_copy(update={"target_finish_offset_workdays": 14})

    evaluation = evaluate_recovery(project, _utility_disruption())

    assert evaluation.recommended_candidate_id is None
    assert all(
        tuple(sorted(candidate.option_ids)) != ("EXPEDITE_PANEL", "INSTALL_SUPPORT")
        for candidate in evaluation.candidates
    )


def test_overlapping_changes_are_skipped_in_enumeration_and_rejected_explicitly():
    project = _parallel_project(
        options=[_shorten("P"), _shorten("P", "SHORTEN_P_AGAIN")]
    )
    evaluation = evaluate_recovery(project, None)
    selections = {tuple(candidate.option_ids) for candidate in evaluation.candidates}

    assert ("SHORTEN_P", "SHORTEN_P_AGAIN") not in selections
    assert ("SHORTEN_P",) in selections

    with pytest.raises(PolicyError) as exc:
        evaluate_recovery(
            project,
            None,
            option_ids=["SHORTEN_P", "SHORTEN_P_AGAIN"],
        )
    assert exc.value.code == "OVERLAPPING_OPTIONS"


def test_duplicate_explicit_option_is_rejected():
    project = _utility_project()
    with pytest.raises(PolicyError) as exc:
        evaluate_recovery(
            project,
            _utility_disruption(),
            option_ids=["EXPEDITE_PANEL", "EXPEDITE_PANEL"],
        )
    assert exc.value.code == "DUPLICATE_OPTION"


def test_candidate_ids_and_ranking_are_invariant_to_catalogue_and_task_order():
    options = _utility_catalogue()
    disruption = _utility_disruption()
    forward = _utility_project(catalogue=options)
    reversed_catalogue = _utility_project(catalogue=list(reversed(options)))
    reversed_tasks = forward.model_copy(update={"tasks": list(reversed(forward.tasks))})

    evaluations = [
        evaluate_recovery(forward, disruption),
        evaluate_recovery(reversed_catalogue, disruption),
        evaluate_recovery(reversed_tasks, disruption),
    ]
    id_maps = [
        {tuple(sorted(candidate.option_ids)): candidate.candidate_id for candidate in evaluation.candidates}
        for evaluation in evaluations
    ]

    assert id_maps[0] == id_maps[1] == id_maps[2]
    assert evaluations[0].recommended_candidate_id == evaluations[1].recommended_candidate_id
    assert evaluations[0].ranked_feasible_candidate_ids == evaluations[1].ranked_feasible_candidate_ids


def test_candidate_ids_are_unique_with_delimiter_containing_option_ids():
    project = ProjectInput(
        project_id="ID-COLLISION-DEMO",
        version=1,
        start_offset_workdays=0,
        target_finish_offset_workdays=3,
        budget_pence=1_000,
        tasks=[
            TaskInput(id="X", name="Branch X", duration_workdays=3),
            TaskInput(id="Y", name="Branch Y", duration_workdays=3),
        ],
        catalogue=[
            RecoveryOption(
                option_id="A+B",
                changes=[TaskDurationChange(task_id="X", new_duration_workdays=2)],
                incremental_cost_pence=100,
            ),
            RecoveryOption(
                option_id="A",
                changes=[TaskDurationChange(task_id="X", new_duration_workdays=2)],
                incremental_cost_pence=100,
            ),
            RecoveryOption(
                option_id="B",
                changes=[TaskDurationChange(task_id="Y", new_duration_workdays=2)],
                incremental_cost_pence=100,
            ),
        ],
    )

    evaluation = evaluate_recovery(project, None)
    ids = [candidate.candidate_id for candidate in evaluation.candidates]

    assert len(ids) == len(set(ids))
    assert evaluation.candidate_for(["A+B"]).candidate_id != evaluation.candidate_for(["A", "B"]).candidate_id

    reversed_evaluation = evaluate_recovery(
        project.model_copy(update={"catalogue": list(reversed(project.catalogue))}),
        None,
    )
    assert {
        tuple(sorted(candidate.option_ids)): candidate.candidate_id
        for candidate in evaluation.candidates
    } == {
        tuple(sorted(candidate.option_ids)): candidate.candidate_id
        for candidate in reversed_evaluation.candidates
    }
    assert evaluation.ranked_feasible_candidate_ids == reversed_evaluation.ranked_feasible_candidate_ids

    tie_ids = [
        candidate.candidate_id
        for candidate in evaluation.candidates
        if candidate.cost_pence == 100 and candidate.finish_offset_workdays == 3
    ]
    ranked_ties = [
        candidate_id
        for candidate_id in evaluation.ranked_feasible_candidate_ids
        if candidate_id in tie_ids
    ]
    assert ranked_ties == sorted(tie_ids)


def test_explicit_empty_selection_evaluates_the_no_change_candidate():
    evaluation = evaluate_recovery(
        _utility_project(),
        _utility_disruption(),
        option_ids=[],
    )
    candidate = evaluation.candidate_for([])
    assert candidate.option_ids == []
    assert candidate.finish_offset_workdays == 17
    assert candidate.cost_pence == 0


def test_unknown_disruption_task_is_rejected():
    with pytest.raises(PolicyError) as exc:
        evaluate_recovery(
            _utility_project(),
            Disruption(task_id="NOT_A_TASK", revised_duration_workdays=2),
        )
    assert exc.value.code == "UNKNOWN_TASK"
