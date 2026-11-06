"""Phase 2 allowlisted-tool tests.

Tools are read-only projections of the verified evaluation. The model never
receives a mutating tool, and invalid names/arguments/IDs fail closed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from delivery_recovery import Disruption, ProjectInput, RecoveryOption, TaskDurationChange, TaskInput, evaluate_recovery
from delivery_recovery.workflow.tools import TOOL_SPECS, ToolError, execute_tool

FIXTURES = Path(__file__).parent / "fixtures" / "delivery_recovery"


def _utility_evaluation():
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
    return evaluate_recovery(project, disruption)


def test_tool_registry_is_read_only():
    assert set(TOOL_SPECS) == {
        "get_schedule_facts",
        "compare_candidates",
        "explain_constraint",
    }
    assert all(spec.read_only for spec in TOOL_SPECS.values())
    assert not {"set_budget", "delete_dependency", "approve_plan"} & set(TOOL_SPECS)


def test_get_schedule_facts_returns_verified_facts():
    evaluation = _utility_evaluation()

    facts = execute_tool(evaluation, "get_schedule_facts", {"phase": "disrupted"})

    assert facts["finish_offset_workdays"] == 17
    assert facts["critical_task_ids"] == ["A", "C", "D", "E", "G"]
    assert facts["source"] == "deterministic_cpm"


def test_compare_candidates_validates_and_returns_requested_ids():
    evaluation = _utility_evaluation()
    expedite = evaluation.candidate_for(["EXPEDITE_PANEL"])
    support = evaluation.candidate_for(["INSTALL_SUPPORT"])

    result = execute_tool(
        evaluation,
        "compare_candidates",
        {"candidate_ids": [expedite.candidate_id, support.candidate_id]},
    )

    returned = {item["candidate_id"] for item in result["candidates"]}
    assert returned == {expedite.candidate_id, support.candidate_id}
    assert all("finish_offset_workdays" in item for item in result["candidates"])


def test_explain_constraint_returns_rejection_reasons_without_mutation():
    evaluation = _utility_evaluation()
    support = evaluation.candidate_for(["INSTALL_SUPPORT"])
    before = evaluation.model_dump()

    result = execute_tool(
        evaluation,
        "explain_constraint",
        {"candidate_id": support.candidate_id},
    )

    assert result["candidate_id"] == support.candidate_id
    assert result["feasible"] is False
    assert result["rejection_codes"] == ["TARGET_MISSED"]
    assert evaluation.model_dump() == before


def test_unknown_candidate_id_fails_closed():
    evaluation = _utility_evaluation()

    with pytest.raises(ToolError) as exc:
        execute_tool(evaluation, "explain_constraint", {"candidate_id": "CAND-not-real"})

    assert exc.value.code == "UNKNOWN_CANDIDATE"


def test_forbidden_tool_fails_closed():
    evaluation = _utility_evaluation()

    with pytest.raises(ToolError) as exc:
        execute_tool(evaluation, "delete_dependency", {"task_id": "C"})

    assert exc.value.code == "TOOL_NOT_ALLOWED"


def test_extra_or_invalid_tool_arguments_fail_closed():
    evaluation = _utility_evaluation()

    with pytest.raises(ToolError) as extra:
        execute_tool(evaluation, "get_schedule_facts", {"phase": "baseline", "budget": 1})
    assert extra.value.code == "INVALID_TOOL_ARGUMENTS"

    with pytest.raises(ToolError) as invalid:
        execute_tool(evaluation, "get_schedule_facts", {"phase": "future"})
    assert invalid.value.code == "INVALID_TOOL_ARGUMENTS"


def test_duplicate_candidate_ids_are_rejected():
    evaluation = _utility_evaluation()
    candidate = evaluation.candidate_for(["EXPEDITE_PANEL"])

    with pytest.raises(ToolError) as exc:
        execute_tool(
            evaluation,
            "compare_candidates",
            {"candidate_ids": [candidate.candidate_id, candidate.candidate_id]},
        )

    assert exc.value.code == "INVALID_TOOL_ARGUMENTS"
