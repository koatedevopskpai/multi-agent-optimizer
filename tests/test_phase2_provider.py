"""Phase 2 provider-boundary tests with deterministic fakes."""

from __future__ import annotations

from delivery_recovery.workflow.providers import (
    FakeClock,
    FakeProvider,
    ProviderAuthError,
    ProviderLimits,
    ProviderRequest,
    ProviderResponse,
    ProviderRunner,
    ProviderSchemaError,
    ProviderThrottleError,
    ProviderTimeoutError,
    ProviderTransportError,
    ChatMessage,
)


def _request() -> ProviderRequest:
    return ProviderRequest(
        messages=[ChatMessage(role="user", content="evaluate")],
        tools=[],
        max_output_tokens=200,
        run_id="run-test",
    )


def _response(content: str = "ok") -> ProviderResponse:
    return ProviderResponse(content=content, input_tokens=5, output_tokens=3)


def test_auth_failure_fails_fast_without_retry():
    provider = FakeProvider([ProviderAuthError("bad key")])
    runner = ProviderRunner(
        provider,
        limits=ProviderLimits(max_transport_calls=3),
        clock=FakeClock(),
    )

    outcome = runner.complete(_request())

    assert outcome.ok is False
    assert outcome.reason_code == "PROVIDER_AUTH"
    assert len(provider.calls) == 1


def test_timeout_retries_once_then_succeeds():
    provider = FakeProvider([ProviderTimeoutError("timeout"), _response("recovered")])
    clock = FakeClock()
    runner = ProviderRunner(
        provider,
        limits=ProviderLimits(max_transport_calls=2),
        clock=clock,
    )

    outcome = runner.complete(_request())

    assert outcome.ok is True
    assert outcome.response is not None
    assert outcome.response.content == "recovered"
    assert outcome.transport_attempts == 2
    assert len(provider.calls) == 2


def test_transport_retry_budget_is_bounded():
    provider = FakeProvider(
        [ProviderTimeoutError("timeout"), ProviderTransportError("connection")]
    )
    runner = ProviderRunner(
        provider,
        limits=ProviderLimits(max_transport_calls=2),
        clock=FakeClock(),
    )

    outcome = runner.complete(_request())

    assert outcome.ok is False
    assert outcome.reason_code == "PROVIDER_TRANSPORT"
    assert len(provider.calls) == 2


def test_throttle_is_classified_and_retried_within_budget():
    provider = FakeProvider([ProviderThrottleError("429"), _response("ok")])
    runner = ProviderRunner(
        provider,
        limits=ProviderLimits(max_transport_calls=2),
        clock=FakeClock(),
    )

    outcome = runner.complete(_request())

    assert outcome.ok is True
    assert outcome.transport_attempts == 2
    assert len(provider.calls) == 2


def test_schema_error_is_not_retried_as_transport():
    provider = FakeProvider([ProviderSchemaError("invalid JSON")])
    runner = ProviderRunner(
        provider,
        limits=ProviderLimits(max_transport_calls=3),
        clock=FakeClock(),
    )

    outcome = runner.complete(_request())

    assert outcome.ok is False
    assert outcome.reason_code == "INVALID_MODEL_OUTPUT"
    assert len(provider.calls) == 1


def test_deadline_prevents_a_call_and_returns_explicit_reason():
    provider = FakeProvider([_response()])
    clock = FakeClock()
    clock.advance(10.0)
    runner = ProviderRunner(
        provider,
        limits=ProviderLimits(max_transport_calls=2, max_elapsed_seconds=5.0),
        clock=clock,
    )
    request = ProviderRequest(
        messages=[ChatMessage(role="user", content="evaluate")],
        tools=[],
        max_output_tokens=200,
        run_id="run-test",
        deadline_at=clock.now,
    )

    outcome = runner.complete(request)

    assert outcome.ok is False
    assert outcome.reason_code == "DEADLINE_EXCEEDED"
    assert provider.calls == []


def test_circuit_breaker_is_process_scoped_and_recovers_with_one_probe():
    clock = FakeClock()
    provider = FakeProvider([ProviderTimeoutError("t1"), ProviderTimeoutError("t2")])
    runner = ProviderRunner(
        provider,
        limits=ProviderLimits(
            max_transport_calls=1,
            breaker_threshold=2,
            breaker_reset_seconds=30.0,
        ),
        clock=clock,
    )

    first = runner.complete(_request())
    second = runner.complete(_request())
    blocked = runner.complete(_request())

    assert first.ok is False and second.ok is False
    assert blocked.reason_code == "CIRCUIT_OPEN"
    assert len(provider.calls) == 2

    clock.advance(31.0)
    recovered = runner.complete(_request())

    assert recovered.ok is False  # provider list is exhausted; a probe was attempted
    assert len(provider.calls) == 3


def test_elapsed_deadline_is_checked_after_a_transient_call():
    clock = FakeClock()

    class SlowTimeoutProvider:
        name = "fake"
        model = "slow"

        def complete(self, request):
            clock.advance(6.0)
            raise ProviderTimeoutError("slow timeout")

    runner = ProviderRunner(
        SlowTimeoutProvider(),
        limits=ProviderLimits(max_transport_calls=2, max_elapsed_seconds=5.0),
        clock=clock,
    )

    outcome = runner.complete(_request())

    assert outcome.ok is False
    assert outcome.reason_code == "DEADLINE_EXCEEDED"


def test_non_response_object_is_a_schema_error():
    class BadProvider:
        name = "fake"
        model = "bad"

        def complete(self, request):
            return {"content": "not a ProviderResponse"}

    runner = ProviderRunner(BadProvider(), limits=ProviderLimits(max_transport_calls=2), clock=FakeClock())

    outcome = runner.complete(_request())

    assert outcome.ok is False
    assert outcome.reason_code == "INVALID_MODEL_OUTPUT"


def test_runner_returns_per_attempt_ledger_for_retry_success():
    provider = FakeProvider([ProviderTimeoutError("timeout"), _response("recovered")])
    runner = ProviderRunner(
        provider,
        limits=ProviderLimits(max_transport_calls=2),
        clock=FakeClock(),
    )

    outcome = runner.complete(_request(), before_attempt=lambda: None)

    assert outcome.ok is True
    assert outcome.transport_attempts == 2
    assert [item.ok for item in outcome.attempts] == [False, True]
    assert [item.reason_code for item in outcome.attempts] == [
        "PROVIDER_TIMEOUT",
        None,
    ]


def test_attempt_admission_rejection_is_not_counted_or_transmitted():
    provider = FakeProvider([ProviderTimeoutError("timeout"), _response("unused")])
    runner = ProviderRunner(
        provider,
        limits=ProviderLimits(max_transport_calls=2),
        clock=FakeClock(),
    )
    admissions = {"count": 0}

    def admit_once() -> str | None:
        admissions["count"] += 1
        return None if admissions["count"] == 1 else "TOKEN_BUDGET_EXCEEDED"

    outcome = runner.complete(_request(), before_attempt=admit_once)

    assert outcome.ok is False
    assert outcome.reason_code == "TOKEN_BUDGET_EXCEEDED"
    assert outcome.transport_attempts == 1
    assert len(provider.calls) == 1
    assert len(outcome.attempts) == 1
    assert outcome.attempts[0].ok is False


def test_circuit_open_does_not_invoke_attempt_admission():
    provider = FakeProvider([ProviderTimeoutError("timeout")])
    runner = ProviderRunner(
        provider,
        limits=ProviderLimits(max_transport_calls=1, breaker_threshold=1),
        clock=FakeClock(),
    )

    first = runner.complete(_request())
    admissions = {"count": 0}

    def admit() -> str | None:
        admissions["count"] += 1
        return None

    blocked = runner.complete(_request(), before_attempt=admit)

    assert first.ok is False
    assert blocked.reason_code == "CIRCUIT_OPEN"
    assert blocked.transport_attempts == 0
    assert blocked.attempts == []
    assert admissions["count"] == 0
