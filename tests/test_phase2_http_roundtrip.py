"""End-to-end MockTransport tests for the tool-call message round trip."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from delivery_recovery import Disruption, ProjectInput, RecoveryOption, TaskDurationChange, TaskInput, evaluate_recovery
from delivery_recovery.workflow.contracts import DraftRecoveryRequest
from delivery_recovery.workflow.http_provider import OpenAICompatibleProvider
from delivery_recovery.workflow.service import RecoveryWorkflow

FIXTURES = Path(__file__).parent / "fixtures" / "delivery_recovery"


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


def _draft() -> DraftRecoveryRequest:
    project, disruption = _inputs()
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


def _final_explanation() -> str:
    project, disruption = _inputs()
    evaluation = evaluate_recovery(project, disruption)
    candidate = evaluation.candidate_for(["EXPEDITE_PANEL"])
    return json.dumps(
        {
            "recommended_candidate_id": candidate.candidate_id,
            "summary": "Expedite restores day 15.",
            "finish_offset_workdays": candidate.finish_offset_workdays,
            "cost_pence": candidate.cost_pence,
        }
    )


def _tool_call(call_id: str, phase: str = "disrupted") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "get_schedule_facts",
            "arguments": json.dumps({"phase": phase}),
        },
    }


def test_single_tool_call_id_and_structured_messages_round_trip():
    payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        payloads.append(body)
        if len(payloads) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "", "tool_calls": [_tool_call("call-proof-1")]}}
                    ]
                },
            )

        assistant = next(
            message
            for message in body["messages"]
            if message["role"] == "assistant" and message.get("tool_calls")
        )
        tool_result = next(
            message for message in body["messages"] if message["role"] == "tool"
        )
        assert assistant["tool_calls"][0]["id"] == "call-proof-1"
        assert tool_result["tool_call_id"] == "call-proof-1"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": _final_explanation()}}]},
        )

    provider = OpenAICompatibleProvider(
        model="offline",
        base_url="https://example.test/v1",
        api_key="synthetic",
        transport=httpx.MockTransport(handler),
    )
    result = RecoveryWorkflow(provider).start(_draft())

    assert result.status == "completed"
    assert result.provider_outcome is not None
    assert result.provider_outcome.mode == "model"
    assert len(payloads) == 2


def test_multiple_tool_calls_have_distinct_matching_ids():
    payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        payloads.append(body)
        if len(payloads) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "tool_calls": [
                                    _tool_call("call-baseline", "baseline"),
                                    _tool_call("call-disrupted", "disrupted"),
                                ],
                            }
                        }
                    ]
                },
            )

        assistant = next(
            message
            for message in body["messages"]
            if message["role"] == "assistant" and message.get("tool_calls")
        )
        tool_results = [
            message for message in body["messages"] if message["role"] == "tool"
        ]
        assert [call["id"] for call in assistant["tool_calls"]] == [
            "call-baseline",
            "call-disrupted",
        ]
        assert [message["tool_call_id"] for message in tool_results] == [
            "call-baseline",
            "call-disrupted",
        ]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": _final_explanation()}}]},
        )

    provider = OpenAICompatibleProvider(
        model="offline",
        base_url="https://example.test/v1",
        api_key="synthetic",
        transport=httpx.MockTransport(handler),
    )
    result = RecoveryWorkflow(provider).start(_draft())

    assert result.status == "completed"
    assert len(payloads) == 2
