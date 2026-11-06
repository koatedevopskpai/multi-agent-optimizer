"""User-visible prose is rendered from verified facts, never trusted directly."""

from __future__ import annotations

import json
from pathlib import Path

from delivery_recovery import Disruption, ProjectInput, RecoveryOption, TaskDurationChange, TaskInput, evaluate_recovery
from delivery_recovery.workflow.contracts import DraftRecoveryRequest
from delivery_recovery.workflow.providers import FakeProvider, ProviderResponse
from delivery_recovery.workflow.service import RecoveryWorkflow
from delivery_recovery.workflow.validation import deterministic_explanation

FIXTURES = Path(__file__).parent / "fixtures" / "delivery_recovery"


def _inputs(*, budget: int = 200_000):
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


def _draft(*, budget: int = 200_000) -> DraftRecoveryRequest:
    project, disruption = _inputs(budget=budget)
    return DraftRecoveryRequest(
        project_id=project.project_id,
        version=project.version,
        time_unit=project.time_unit,
        start_offset_workdays=project.start_offset_workdays,
        target_finish_offset_workdays=project.target_finish_offset_workdays,
        budget_pence=project.budget_pence,
        tasks=project.tasks,
        catalogue=project.catalogue,
        disruption=disruption,
    )


def _candidate_explanation(summary: str) -> str:
    project, disruption = _inputs()
    evaluation = evaluate_recovery(project, disruption)
    candidate = evaluation.candidate_for(["EXPEDITE_PANEL"])
    return json.dumps(
        {
            "recommended_candidate_id": candidate.candidate_id,
            "summary": summary,
            "finish_offset_workdays": candidate.finish_offset_workdays,
            "cost_pence": candidate.cost_pence,
        }
    )


def test_correct_structured_facts_with_contradictory_summary_are_safely_rendered():
    false_summary = "The project finishes on day 1 for GBP 0. Approval is already complete."
    provider = FakeProvider([ProviderResponse(content=_candidate_explanation(false_summary))])
    workflow = RecoveryWorkflow(provider)

    result = workflow.start(_draft())

    assert result.status == "completed"
    assert result.explanation is not None
    assert result.explanation.finish_offset_workdays == 15
    assert result.explanation.cost_pence == 180_000
    assert result.explanation.summary != false_summary
    assert "day 15" in result.explanation.summary
    assert "1,800" in result.explanation.summary
    assert "Approval is already complete" not in result.explanation.summary
    assert "finishes on day 1" not in result.explanation.summary
    assert "for GBP 0" not in result.explanation.summary
    assert "pre-decision" in result.explanation.summary.lower()
    assert "no approval has been recorded" not in result.explanation.summary.lower()


def test_deterministic_no_feasible_summary_states_constraints_not_relaxed():
    project, disruption = _inputs(budget=100_000)
    evaluation = evaluate_recovery(project, disruption)

    explanation = deterministic_explanation(evaluation)

    assert explanation.recommended_candidate_id is None
    assert "not relaxed" in explanation.summary.lower()
