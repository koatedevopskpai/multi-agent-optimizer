import time

import pytest

from multi_agent_optimizer import (
    CircuitBreaker,
    CircuitOpenError,
    extract_json_object,
    safe_llm_json,
)
from pydantic import BaseModel


class _Tag(BaseModel):
    tag: str


class _Flaky:
    def __init__(self, fail_times: int):
        self._left = fail_times
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        if self._left > 0:
            self._left -= 1
            raise ValueError("boom")
        return self.calls


def test_retry_succeeds_after_transient_failure(monkeypatch):
    fail = _Flaky(2)

    def retry(fn, attempts, base_delay, factor, exceptions=(Exception,)):
        import multi_agent_optimizer.resilience as r
        return r.retry_with_backoff(fn, attempts, base_delay, factor, exceptions)

    with monkeypatch.context() as m:
        m.setattr("time.sleep", lambda _: None)
        result = retry(fail, attempts=3, base_delay=0.1, factor=2.0)
    assert result == 3  # called 3 times, succeeded on the third


def test_circuit_breaker_opens(monkeypatch):
    breaker = CircuitBreaker(failure_threshold=2, reset_seconds=60)
    boom = lambda: (_ for _ in ()).throw(ValueError("x"))  # noqa: E731
    with pytest.raises(ValueError):
        breaker.call(boom)
    with pytest.raises(ValueError):
        breaker.call(boom)
    # open now
    with pytest.raises(CircuitOpenError):
        breaker.call(lambda: 1)


def test_circuit_breaker_recovers_after_reset(monkeypatch):
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=0)
    with pytest.raises(ValueError):
        breaker.call(lambda: (_ for _ in ()).throw(ValueError("x")))
    # reset_seconds negative path: open_until in the past -> not open
    breaker._open_until = time.monotonic() - 1
    assert breaker.call(lambda: 42) == 42


def test_extract_json_object_from_noisy_text():
    assert extract_json_object("Here you go: {\"a\": 1} done") == '{"a": 1}'
    with pytest.raises(ValueError):
        extract_json_object("no json here")


def test_safe_llm_json_falls_back_on_bad_output():
    class BadLLM:
        def chat(self, system, user):
            return "not json at all"

    breaker = CircuitBreaker()
    result = safe_llm_json(
        BadLLM(),
        _Tag,
        "sys",
        "user",
        fallback=_Tag(tag="deterministic"),
        breaker=breaker,
    )
    assert result.tag == "deterministic"


def test_safe_llm_json_parses_valid_output():
    class GoodLLM:
        def chat(self, system, user):
            return '{"tag": "validated"}'

    breaker = CircuitBreaker()
    result = safe_llm_json(GoodLLM(), _Tag, "sys", "user", fallback=_Tag(tag="x"), breaker=breaker)
    assert result.tag == "validated"