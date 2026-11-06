"""Run the development fake-provider cases; keep the holdout partition frozen."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from delivery_recovery import Disruption, ProjectInput, RecoveryOption, TaskDurationChange, TaskInput, evaluate_recovery
from delivery_recovery.workflow.contracts import DraftRecoveryRequest
from delivery_recovery.workflow.providers import FakeProvider, ProviderResponse, ToolCall
from delivery_recovery.workflow.service import RecoveryWorkflow

ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures" / "delivery_recovery"
CASES = ROOT / "evals" / "phase2"


def _inputs():
    raw = json.loads((FIXTURES / "utility_recovery.json").read_text(encoding="utf-8"))
    project = ProjectInput(
        project_id=raw["project_id"],
        version=raw["project_version"],
        start_offset_workdays=raw["start_offset_workdays"],
        target_finish_offset_workdays=raw["target_finish_offset_workdays"],
        budget_pence=raw["budget_pence"],
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


def _draft(*, budget=200_000) -> DraftRecoveryRequest:
    project, disruption = _inputs()
    return DraftRecoveryRequest(
        project_id=project.project_id,
        version=project.version,
        time_unit=project.time_unit,
        start_offset_workdays=project.start_offset_workdays,
        target_finish_offset_workdays=project.target_finish_offset_workdays,
        budget_pence=budget,
        tasks=project.tasks,
        catalogue=project.catalogue,
        disruption=disruption,
    )


def _valid_explanation() -> str:
    project, disruption = _inputs()
    evaluation = evaluate_recovery(project, disruption)
    candidate = evaluation.candidate_for(["EXPEDITE_PANEL"])
    return json.dumps(
        {
            "recommended_candidate_id": candidate.candidate_id,
            "summary": "Expedite restores day 15 within budget.",
            "finish_offset_workdays": candidate.finish_offset_workdays,
            "cost_pence": candidate.cost_pence,
        }
    )


def _provider_for(kind: str) -> FakeProvider:
    if kind == "happy_path":
        return FakeProvider(
            [
                ProviderResponse(
                    tool_calls=[
                        ToolCall(name="get_schedule_facts", arguments={"phase": "disrupted"})
                    ]
                ),
                ProviderResponse(content=_valid_explanation()),
            ]
        )
    if kind == "clarification":
        return FakeProvider([ProviderResponse(content=_valid_explanation())])
    if kind == "forbidden_tool":
        return FakeProvider(
            [
                ProviderResponse(
                    tool_calls=[
                        ToolCall(name="set_budget", arguments={"budget_pence": 1})
                    ]
                )
            ]
        )
    if kind == "schema_failure":
        return FakeProvider(
            [ProviderResponse(content="not json"), ProviderResponse(content="still not json")]
        )
    raise AssertionError(f"unknown development case kind {kind!r}")


def test_development_cases_run_against_fake_provider():
    cases = json.loads((CASES / "development_cases.json").read_text(encoding="utf-8"))["cases"]

    for case in cases:
        draft = _draft(budget=None) if case["kind"] == "clarification" else _draft()
        result = RecoveryWorkflow(_provider_for(case["kind"])).start(draft)
        assert result.status == case["expected_status"], case["id"]


def test_holdout_partition_is_frozen_and_disjoint():
    development = json.loads((CASES / "development_cases.json").read_text(encoding="utf-8"))
    holdout = json.loads((CASES / "holdout_cases.json").read_text(encoding="utf-8"))

    dev_ids = {case["id"] for case in development["cases"]}
    holdout_ids = {case["id"] for case in holdout["cases"]}

    assert holdout["frozen"] is True
    assert dev_ids.isdisjoint(holdout_ids)
    assert holdout["partition"] == "holdout"
