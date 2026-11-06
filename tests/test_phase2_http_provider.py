"""Offline HTTP provider adapter tests using httpx.MockTransport."""

from __future__ import annotations

import json

import httpx
import pytest

from delivery_recovery.workflow.http_provider import OpenAICompatibleProvider
from delivery_recovery.workflow.providers import (
    ChatMessage,
    FakeClock,
    ProviderAuthError,
    ProviderLimits,
    ProviderRequest,
    ProviderRunner,
    ProviderSchemaError,
    ProviderThrottleError,
    ProviderTimeoutError,
    ProviderTransportError,
)


def _request() -> ProviderRequest:
    return ProviderRequest(
        messages=[ChatMessage(role="user", content="evaluate")],
        tools=[
            {
                "name": "get_schedule_facts",
                "description": "facts",
                "parameters": {"type": "object"},
            }
        ],
        max_output_tokens=250,
        run_id="http-run",
    )


def _provider(handler) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        model="test-model",
        base_url="https://example.test/v1",
        api_key="secret-not-for-traces",
        transport=httpx.MockTransport(handler),
    )


def _ok_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "content": "verified",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "get_schedule_facts",
                                    "arguments": '{"phase": "baseline"}',
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        },
    )


def test_valid_response_is_parsed_without_network_or_hidden_retry():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _ok_response()

    provider = _provider(handler)
    response = provider.complete(_request())

    assert response.content == "verified"
    assert response.tool_calls[0].name == "get_schedule_facts"
    assert response.tool_calls[0].arguments == {"phase": "baseline"}
    assert response.input_tokens == 11
    assert response.output_tokens == 7
    assert len(calls) == 1
    assert calls[0].headers["Authorization"] == "Bearer secret-not-for-traces"


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, ProviderAuthError),
        (403, ProviderAuthError),
        (429, ProviderThrottleError),
        (500, ProviderTransportError),
        (400, ProviderSchemaError),
    ],
)
def test_http_statuses_are_classified(status, error_type):
    provider = _provider(lambda request: httpx.Response(status, json={"error": "x"}))

    with pytest.raises(error_type):
        provider.complete(_request())


def test_timeout_and_transport_exceptions_are_classified():
    def timeout_handler(request):
        raise httpx.TimeoutException("timed out")

    with pytest.raises(ProviderTimeoutError):
        _provider(timeout_handler).complete(_request())

    def transport_handler(request):
        raise httpx.ConnectError("no route")

    with pytest.raises(ProviderTransportError):
        _provider(transport_handler).complete(_request())


def test_malformed_json_and_tool_arguments_are_schema_errors():
    malformed = _provider(
        lambda request: httpx.Response(200, content=b"not json", headers={"content-type": "application/json"})
    )
    with pytest.raises(ProviderSchemaError):
        malformed.complete(_request())

    bad_arguments = _provider(
        lambda request: httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "get_schedule_facts",
                                        "arguments": "{not json",
                                    }
                                }
                            ],
                        }
                    }
                ]
            },
        )
    )
    with pytest.raises(ProviderSchemaError):
        bad_arguments.complete(_request())


def test_runner_retry_does_not_multiply_hidden_transport_retries():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "slow down"})
        return _ok_response()

    provider = _provider(handler)
    runner = ProviderRunner(
        provider,
        limits=ProviderLimits(max_transport_calls=2),
        clock=FakeClock(),
    )

    outcome = runner.complete(_request())

    assert outcome.ok is True
    assert len(calls) == 2
    assert outcome.transport_attempts == 2


@pytest.mark.parametrize("status", [408, 504])
def test_gateway_timeout_statuses_are_timeouts(status):
    provider = _provider(lambda request: httpx.Response(status, json={"error": "timeout"}))
    with pytest.raises(ProviderTimeoutError):
        provider.complete(_request())


def test_unlisted_client_error_is_a_schema_error():
    provider = _provider(lambda request: httpx.Response(418, json={"error": "teapot"}))
    with pytest.raises(ProviderSchemaError):
        provider.complete(_request())


def test_tool_calls_must_be_a_list():
    provider = _provider(
        lambda request: httpx.Response(
            200,
            json={"choices": [{"message": {"content": "", "tool_calls": {"bad": "shape"}}}]},
        )
    )
    with pytest.raises(ProviderSchemaError):
        provider.complete(_request())


@pytest.mark.parametrize(
    "tool_call",
    [
        {"function": {"name": "get_schedule_facts", "arguments": "[1, 2]"}},
        {"function": {"name": "", "arguments": "{}"}},
    ],
)
def test_invalid_tool_shape_is_a_schema_error(tool_call):
    provider = _provider(
        lambda request: httpx.Response(
            200,
            json={"choices": [{"message": {"content": "", "tool_calls": [tool_call]}}]},
        )
    )
    with pytest.raises(ProviderSchemaError):
        provider.complete(_request())

def test_http_timeout_is_clamped_to_request_remaining_time():
    captured = {}

    class FakeClient:
        def post(self, url, *, json, headers, timeout):
            captured["timeout"] = timeout
            return _ok_response()

    provider = _provider(lambda request: _ok_response())
    provider._client = FakeClient()
    request = _request().model_copy(update={"remaining_seconds": 1.5})

    provider.complete(request)

    assert captured["timeout"] == 1.5


def test_negative_usage_is_a_schema_error():
    provider = _provider(
        lambda request: httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": -1, "completion_tokens": 2},
            },
        )
    )
    with pytest.raises(ProviderSchemaError):
        provider.complete(_request())


def test_retry_recalculates_remaining_timeout_before_each_attempt():
    clock = FakeClock(100.0)
    seen: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeout = float(request.extensions["timeout"]["read"])
        seen.append(timeout)
        if len(seen) == 1:
            clock.advance(4.0)
            return httpx.Response(429, json={"error": "slow down"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    provider = _provider(handler)
    runner = ProviderRunner(
        provider,
        limits=ProviderLimits(max_transport_calls=2, max_elapsed_seconds=5.0),
        clock=clock,
    )
    request = _request().model_copy(
        update={"deadline_at": 105.0, "remaining_seconds": 5.0}
    )

    outcome = runner.complete(request)

    assert outcome.ok is True
    assert len(seen) == 2
    assert 4.9 <= seen[0] <= 5.0
    assert 0.9 <= seen[1] <= 1.0


def test_no_retry_starts_after_deadline_is_exhausted():
    clock = FakeClock(100.0)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        clock.advance(5.0)
        return httpx.Response(429, json={"error": "slow down"})

    provider = _provider(handler)
    runner = ProviderRunner(
        provider,
        limits=ProviderLimits(max_transport_calls=2, max_elapsed_seconds=5.0),
        clock=clock,
    )
    request = _request().model_copy(
        update={"deadline_at": 105.0, "remaining_seconds": 5.0}
    )

    outcome = runner.complete(request)

    assert outcome.ok is False
    assert outcome.reason_code == "DEADLINE_EXCEEDED"
    assert calls == 1
import json
from delivery_recovery.workflow.http_provider import OpenAICompatibleProvider
import httpx


def test_openai_compatible_provider_sends_explicit_thinking_disabled_payload():
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    provider = OpenAICompatibleProvider(
        model="deepseek-flash",
        base_url="https://api.deepseek.com",
        api_key="synthetic",
        transport=httpx.MockTransport(handler),
        thinking={"type": "disabled"},
    )
    from delivery_recovery.workflow.providers import ChatMessage, ProviderRequest

    provider.complete(
        ProviderRequest(
            messages=[ChatMessage(role="user", content="test")],
            run_id="payload-test",
        )
    )

    assert payloads[0]["thinking"] == {"type": "disabled"}
    assert payloads[0]["model"] == "deepseek-flash"
