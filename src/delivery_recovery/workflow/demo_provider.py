"""Deterministic local provider used by the Phase 3 demo worker.

It is intentionally labelled as a fake provider and makes no network call.
"""

from __future__ import annotations

import json

from .providers import ProviderRequest, ProviderResponse

__all__ = ["LocalDemoProvider"]


class LocalDemoProvider:
    name = "local-demo"
    model = "deterministic-template"
    fake_provider = True

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        payload: dict = {}
        for message in reversed(request.messages):
            if message.role == "user":
                try:
                    payload = json.loads(message.content)
                except (TypeError, ValueError):
                    payload = {}
                break
        candidate_id = payload.get("recommended_candidate_id")
        candidates = payload.get("candidates") or []
        candidate = next(
            (item for item in candidates if item.get("candidate_id") == candidate_id),
            None,
        )
        if candidate is None:
            content = json.dumps(
                {
                    "recommended_candidate_id": None,
                    "summary": "No feasible catalogue option meets the stated target and budget.",
                }
            )
        else:
            content = json.dumps(
                {
                    "recommended_candidate_id": candidate["candidate_id"],
                    "summary": "Verified deterministic recovery candidate.",
                    "finish_offset_workdays": candidate["finish_offset_workdays"],
                    "cost_pence": candidate["cost_pence"],
                }
            )
        return ProviderResponse(
            content=content,
            input_tokens=max(1, len(json.dumps(payload)) // 4),
            output_tokens=max(1, len(content) // 4),
            usage_reported=True,
        )
