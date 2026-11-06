"""Boundary tests for draft completion and semantic explanation validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from delivery_recovery import Disruption, ProjectInput, RecoveryOption, TaskDurationChange, TaskInput, evaluate_recovery
from delivery_recovery.workflow.contracts import DraftRecoveryRequest, Explanation
from delivery_recovery.workflow.validation import deterministic_explanation, validate_explanation

FIXTURES = Path(__file__).parent / "fixtures" / "delivery_recovery"


def _project(*, budget: int = 200_000) -> tuple[ProjectInput, Disruption]:
    raw = json.loads((FIXTURES / "utility_recovery.json").read_text(encoding="utf-8"))
    project = ProjectInput(
        project_id=raw["project_id"],
        version=raw["project_version"],
        start_offset_workdays=raw["start_offset_workdays"],
        target_finish_offset_workdays=raw["target_finish_offset_workdays"],
        budget_pence=budget,
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
    return project, disruption


def _draft(**overrides) -> DraftRecoveryRequest:
    project, disruption = _project()
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


def test_draft_reports_all_missing_facts_and_refuses_conversion():
    draft = DraftRecoveryRequest()

    assert [fact.field for fact in draft.missing_facts()] == [
        "time_unit",
        "target_finish_offset_workdays",
        "budget_pence",
        "tasks",
    ]
    with pytest.raises(ValueError, match="missing required facts"):
        draft.to_project_input()


def test_explanation_requires_finish_and_cost_for_a_candidate():
    with pytest.raises(ValidationError):
        Explanation(
            recommended_candidate_id="CAND-1",
            summary="missing numeric facts",
        )


def test_explanation_rejects_numeric_facts_when_no_candidate():
    with pytest.raises(ValidationError):
        Explanation(
            recommended_candidate_id=None,
            summary="no candidate but invented facts",
            finish_offset_workdays=15,
            cost_pence=180_000,
        )


def test_validate_explanation_rejects_mismatched_and_unknown_candidates():
    project, disruption = _project()
    evaluation = evaluate_recovery(project, disruption)
    candidate = evaluation.candidate_for(["EXPEDITE_PANEL"])

    mismatched = Explanation(
        recommended_candidate_id=None,
        summary="No candidate",
    )
    assert any("does not match" in error for error in validate_explanation(mismatched, evaluation))

    unknown = Explanation(
        recommended_candidate_id="CAND-unknown",
        summary="Unknown candidate",
        finish_offset_workdays=15,
        cost_pence=180_000,
    )
    assert any("not a known candidate" in error for error in validate_explanation(unknown, evaluation))

    valid = Explanation(
        recommended_candidate_id=candidate.candidate_id,
        summary="Expedite restores day 15",
        finish_offset_workdays=candidate.finish_offset_workdays,
        cost_pence=candidate.cost_pence,
    )
    assert validate_explanation(valid, evaluation) == []


def test_validate_explanation_rejects_a_rejected_candidate():
    project, disruption = _project()
    evaluation = evaluate_recovery(project, disruption)
    support = evaluation.candidate_for(["INSTALL_SUPPORT"])

    explanation = Explanation(
        recommended_candidate_id=support.candidate_id,
        summary="Installation support reaches day 16",
        finish_offset_workdays=support.finish_offset_workdays,
        cost_pence=support.cost_pence,
    )

    errors = validate_explanation(explanation, evaluation)
    assert any("does not match" in error for error in errors)
    assert any("rejected candidate cannot be recommended" in error for error in errors)


def test_deterministic_fallback_handles_no_feasible_candidate():
    project, disruption = _project(budget=100_000)
    evaluation = evaluate_recovery(project, disruption)

    explanation = deterministic_explanation(evaluation)

    assert explanation.recommended_candidate_id is None
    assert explanation.finish_offset_workdays is None
    assert explanation.cost_pence is None
    assert validate_explanation(explanation, evaluation) == []
