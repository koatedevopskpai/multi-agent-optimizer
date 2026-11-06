"""Opt-in DeepSeek configuration and session spending controls.

The default workflow remains offline. This module is only used by the explicit
live-acceptance command after access and spending authority are established.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .http_provider import OpenAICompatibleProvider
from .providers import (
    ProviderAdmissionError,
    ProviderBudgetError,
    ProviderRequest,
    ProviderResponse,
)

__all__ = [
    "BudgetReservation",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "LiveBudget",
    "build_deepseek_provider",
]

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-flash"
PRICING_DATE = "2026-11-02"
# Conservative peak rates from the official pricing page, USD per 1M tokens.
PEAK_INPUT_USD_PER_MILLION = 0.30
PEAK_OUTPUT_USD_PER_MILLION = 1.20
USD_TO_GBP_CONSERVATIVE = 1.0


def build_deepseek_provider(
    api_key: str,
    *,
    timeout_seconds: float = 30.0,
    transport=None,
) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        model=DEEPSEEK_MODEL,
        base_url=DEEPSEEK_BASE_URL,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
        transport=transport,
        thinking={"type": "disabled"},
    )


@dataclass
class BudgetReservation:
    input_tokens: int
    output_tokens: int
    cost_usd: float


@dataclass
class LiveBudget:
    max_attempts: int = 10
    max_input_tokens: int = 30_000
    max_output_tokens_per_request: int = 2_048
    max_output_tokens_total: int = 4_096
    max_cost_gbp: float = 5.0
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    records: list[dict[str, Any]] = field(default_factory=list)

    def _estimate_input(self, request: ProviderRequest) -> int:
        payload = {
            "messages": [message.model_dump(mode="json") for message in request.messages],
            "tools": request.tools,
        }
        return max(1, len(json.dumps(payload, sort_keys=True)) // 4)

    def _cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens / 1_000_000 * PEAK_INPUT_USD_PER_MILLION
            + output_tokens / 1_000_000 * PEAK_OUTPUT_USD_PER_MILLION
        )

    def admit(self, request: ProviderRequest) -> BudgetReservation:
        if request.max_output_tokens > self.max_output_tokens_per_request:
            raise ProviderBudgetError(
                f"requested output {request.max_output_tokens} exceeds "
                f"{self.max_output_tokens_per_request}"
            )
        if self.attempts >= self.max_attempts:
            raise ProviderBudgetError("session transport-attempt budget exhausted")

        estimated_input = self._estimate_input(request)
        reserved_output = request.max_output_tokens
        projected_input = self.input_tokens + estimated_input
        projected_output = self.output_tokens + reserved_output
        projected_cost = self._cost(projected_input, projected_output)

        if projected_input > self.max_input_tokens:
            raise ProviderBudgetError("session input-token budget would be exceeded")
        if projected_output > self.max_output_tokens_total:
            raise ProviderBudgetError("session output-token budget would be exceeded")
        if projected_cost * USD_TO_GBP_CONSERVATIVE > self.max_cost_gbp:
            raise ProviderBudgetError("session GBP cost ceiling would be exceeded")

        self.attempts += 1
        return BudgetReservation(
            input_tokens=estimated_input,
            output_tokens=reserved_output,
            cost_usd=projected_cost,
        )

    def settle(
        self,
        reservation: BudgetReservation,
        response: ProviderResponse,
    ) -> None:
        if response.usage_reported:
            input_tokens = response.input_tokens
            output_tokens = response.output_tokens
            usage_source = "reported"
        else:
            input_tokens = reservation.input_tokens
            output_tokens = reservation.output_tokens
            usage_source = "reserved"
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cost_usd += self._cost(input_tokens, output_tokens)
        self.records.append(
            {
                "attempt": self.attempts,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "usage_source": usage_source,
                "estimated_cost_usd": round(self._cost(input_tokens, output_tokens), 8),
                "pricing_date": PRICING_DATE,
                "currency": "USD",
            }
        )

    def fail(self, reservation: BudgetReservation) -> None:
        # A failed transport attempt still consumes its worst-case reservation.
        self.input_tokens += reservation.input_tokens
        self.output_tokens += reservation.output_tokens
        self.cost_usd += self._cost(reservation.input_tokens, reservation.output_tokens)
        self.records.append(
            {
                "attempt": self.attempts,
                "input_tokens": reservation.input_tokens,
                "output_tokens": reservation.output_tokens,
                "usage_source": "reserved_on_failure",
                "estimated_cost_usd": round(
                    self._cost(reservation.input_tokens, reservation.output_tokens), 8
                ),
                "pricing_date": PRICING_DATE,
                "currency": "USD",
            }
        )

    def summary(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost_usd": round(self.cost_usd, 8),
            "estimated_cost_gbp_conservative": round(
                self.cost_usd * USD_TO_GBP_CONSERVATIVE, 8
            ),
            "max_attempts": self.max_attempts,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens_per_request": self.max_output_tokens_per_request,
            "max_output_tokens_total": self.max_output_tokens_total,
            "max_cost_gbp": self.max_cost_gbp,
            "pricing_date": PRICING_DATE,
            "pricing_note": (
                "Peak DeepSeek deepseek-flash rates, USD 0.30/M input and "
                "USD 1.20/M output; conservative 1 USD = 1 GBP. Estimated, "
                "not reconciled billing."
            ),
            "records": self.records,
        }


class BudgetedProvider:
    name = "budgeted"

    def __init__(self, provider, budget: LiveBudget) -> None:
        self.provider = provider
        self.budget = budget
        self.name = provider.name
        self.model = provider.model

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        try:
            reservation = self.budget.admit(request)
        except ProviderBudgetError as exc:
            # Admission happens before the inner provider is called. Signal the
            # runner explicitly so it does not create a transport ledger entry.
            raise ProviderAdmissionError(str(exc)) from exc
        try:
            response = self.provider.complete(request)
        except Exception:
            self.budget.fail(reservation)
            raise
        self.budget.settle(reservation, response)
        return response
