"""Offline tests for the opt-in DeepSeek configuration and spending controls."""

from __future__ import annotations

import pytest

from delivery_recovery.workflow.live import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    BudgetedProvider,
    LiveBudget,
    build_deepseek_provider,
)
from delivery_recovery.workflow.providers import (
    ChatMessage,
    FakeProvider,
    ProviderBudgetError,
    ProviderRequest,
    ProviderResponse,
)


def _request(*, max_output_tokens: int = 100) -> ProviderRequest:
    return ProviderRequest(
        messages=[ChatMessage(role="user", content="synthetic")],
        max_output_tokens=max_output_tokens,
        run_id="live-budget-test",
    )


def test_deepseek_provider_is_explicitly_non_thinking():
    provider = build_deepseek_provider("synthetic-key")

    assert provider.model == DEEPSEEK_MODEL
    assert provider.base_url == DEEPSEEK_BASE_URL
    assert provider.thinking == {"type": "disabled"}


def test_budgeted_provider_counts_attempts_and_reconciles_reported_usage():
    provider = FakeProvider(
        [
            ProviderResponse(content="ok", input_tokens=10, output_tokens=5),
            ProviderResponse(content="ok", input_tokens=20, output_tokens=7),
        ]
    )
    budget = LiveBudget(max_attempts=2, max_output_tokens_total=1_000)
    wrapped = BudgetedProvider(provider, budget)

    wrapped.complete(_request())
    wrapped.complete(_request())

    summary = budget.summary()
    assert summary["attempts"] == 2
    assert summary["input_tokens"] == 30
    assert summary["output_tokens"] == 12
    assert all(record["usage_source"] == "reported" for record in summary["records"])


def test_missing_usage_is_charged_at_reservation():
    provider = FakeProvider(
        [ProviderResponse(content="ok", usage_reported=False)]
    )
    budget = LiveBudget(max_attempts=1, max_output_tokens_total=1_000)
    wrapped = BudgetedProvider(provider, budget)

    wrapped.complete(_request(max_output_tokens=50))

    record = budget.summary()["records"][0]
    assert record["usage_source"] == "reserved"
    assert record["output_tokens"] == 50
    assert record["input_tokens"] > 0


def test_attempt_budget_stops_before_provider_call():
    provider = FakeProvider([ProviderResponse(content="ok")])
    budget = LiveBudget(max_attempts=1, max_output_tokens_total=1_000)
    wrapped = BudgetedProvider(provider, budget)
    wrapped.complete(_request())

    with pytest.raises(ProviderBudgetError):
        wrapped.complete(_request())

    assert len(provider.calls) == 1


def test_per_request_output_and_cost_admission_are_enforced():
    provider = FakeProvider([])
    budget = LiveBudget(max_attempts=2, max_output_tokens_per_request=100)
    wrapped = BudgetedProvider(provider, budget)

    with pytest.raises(ProviderBudgetError):
        wrapped.complete(_request(max_output_tokens=101))


def test_failed_transport_consumes_reservation():
    provider = FakeProvider([RuntimeError("transport failed")])
    budget = LiveBudget(max_attempts=1, max_output_tokens_total=1_000)
    wrapped = BudgetedProvider(provider, budget)

    with pytest.raises(RuntimeError):
        wrapped.complete(_request(max_output_tokens=40))

    record = budget.summary()["records"][0]
    assert record["usage_source"] == "reserved_on_failure"
    assert record["output_tokens"] == 40
