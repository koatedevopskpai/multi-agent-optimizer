"""M02: adversarial provider behaviour must not change scheduling facts.

Covers invalid structured output, timeouts, rate limits, repeated provider
failure, circuit opening/recovery with a controlled clock, and the core vs
optional graph path.  No network access and no paid calls: every provider is
a local fake.
"""

from __future__ import annotations

import urllib.error

import pytest
from pydantic import BaseModel, ValidationError

from multi_agent_optimizer import (
    CircuitBreaker,
    CircuitOpenError,
    MultiAgentOptimizer,
    NoopLLM,
    ProjectError,
    parse_validated,
    retry_with_backoff,
    safe_llm_json,
)

PROJECT = {
    "A": {"duration": 3},
    "B": {"duration": 2, "dependencies": ["A"]},
    "C": {"duration": 4, "dependencies": ["A"]},
    "D": {"duration": 2, "dependencies": ["B"]},
    "E": {"duration": 1, "dependencies": ["C"]},
    "F": {"duration": 3, "dependencies": ["D", "E"]},
}

VALID_REPLY = '{"notes": "recovered", "note": "recovered", "thought": "recovered"}'

# All three agent schemas accept a payload with these keys; each model ignores
# the keys it does not declare.
EXPECTED_DURATION = 11
EXPECTED_CRITICAL = ["A", "C", "E", "F"]


class _Tag(BaseModel):
    tag: str


class FakeClock:
    """Deterministic monotonic clock for circuit-breaker tests."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _timeout() -> Exception:
    return TimeoutError("provider timed out")


def _rate_limit() -> Exception:
    return urllib.error.HTTPError(
        "https://provider.example.test/v1/chat", 429, "Too Many Requests", None, None
    )


def _connection_reset() -> Exception:
    return ConnectionResetError("connection reset by peer")


class FailingLLM:
    """A provider that always fails with a chosen exception."""

    def __init__(self, error_factory):
        self._error_factory = error_factory
        self.calls = 0

    def chat(self, system: str, user: str) -> str:
        self.calls += 1
        raise self._error_factory()


class ToggleLLM:
    """A provider that fails until switched to a healthy mode."""

    def __init__(self, error_factory):
        self._error_factory = error_factory
        self.calls = 0
        self.healthy = False

    def chat(self, system: str, user: str) -> str:
        self.calls += 1
        if not self.healthy:
            raise self._error_factory()
        return VALID_REPLY


class StaticLLM:
    """A provider that always returns one fixed string."""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0

    def chat(self, system: str, user: str) -> str:
        self.calls += 1
        return self.reply


def _fallback_result(llm) -> tuple[str, int]:
    breaker = CircuitBreaker()
    result = safe_llm_json(
        llm,
        _Tag,
        "sys",
        "user",
        fallback=_Tag(tag="deterministic"),
        breaker=breaker,
    )
    return result.tag, llm.calls


# ---------------------------------------------------------------------------
# Invalid structured output
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        "not json at all",
        "",
        "[]",
        '{"tag": null}',
        '{"other": "x"}',          # missing required field
        '{"tag": "ok"',            # truncated object
        '{"tag": 1} {"tag": 2}',   # two objects: extraction merges them
    ],
)
def test_invalid_structured_output_falls_back(reply):
    tag, calls = _fallback_result(StaticLLM(reply))
    assert tag == "deterministic"
    # retry_with_backoff attempts twice per logical call
    assert calls == 2


def test_valid_output_wrapped_in_prose_is_extracted():
    tag, calls = _fallback_result(StaticLLM('Sure: {"tag": "valid"} — done.'))
    assert tag == "valid"
    assert calls == 1


def test_parse_validated_rejects_invalid_output_directly():
    with pytest.raises(ValidationError):
        parse_validated(StaticLLM('{"tag": null}'), _Tag, "sys", "user")
    with pytest.raises(ValueError):
        parse_validated(StaticLLM("no object here"), _Tag, "sys", "user")


# ---------------------------------------------------------------------------
# Timeouts, rate limits, repeated provider failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error_factory,name",
    [
        (_timeout, "timeout"),
        (_rate_limit, "rate_limit"),
        (_connection_reset, "connection_reset"),
    ],
)
def test_provider_errors_fall_back_after_two_attempts(error_factory, name):
    llm = FailingLLM(error_factory)
    tag, calls = _fallback_result(llm)
    assert tag == "deterministic", name
    assert calls == 2, name


def test_retry_with_backoff_uses_injected_sleep_and_delays():
    delays: list[float] = []
    attempts = {"n": 0}

    def always_fails():
        attempts["n"] += 1
        raise TimeoutError("down")

    with pytest.raises(TimeoutError):
        retry_with_backoff(
            always_fails, attempts=3, base_delay=0.5, factor=3.0, sleep=delays.append
        )
    assert attempts["n"] == 3
    assert delays == [0.5, 1.5]


# ---------------------------------------------------------------------------
# Circuit opening and recovery with controlled time
# ---------------------------------------------------------------------------


def _boom():
    raise ConnectionError("provider down")


def test_circuit_opens_after_threshold_and_skips_calls():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=3, reset_seconds=30, clock=clock)
    calls = {"n": 0}

    def counted_boom():
        calls["n"] += 1
        raise ConnectionError("provider down")

    for _ in range(3):
        with pytest.raises(ConnectionError):
            breaker.call(counted_boom)
    assert calls["n"] == 3
    assert breaker.is_open
    assert breaker.failure_count == 3

    with pytest.raises(CircuitOpenError):
        breaker.call(counted_boom)
    assert calls["n"] == 3  # the failing function was not called again

    clock.advance(29.9)
    assert breaker.is_open
    with pytest.raises(CircuitOpenError):
        breaker.call(counted_boom)

    clock.advance(0.1)  # exactly reset_seconds later
    assert not breaker.is_open
    assert breaker.call(lambda: 42) == 42
    assert breaker.failure_count == 0


def test_failed_probe_after_reset_reopens_the_circuit():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=10, clock=clock)
    with pytest.raises(ConnectionError):
        breaker.call(_boom)
    assert breaker.is_open

    clock.advance(10)
    with pytest.raises(ConnectionError):
        breaker.call(_boom)
    assert breaker.is_open
    assert breaker.failure_count == 2

    clock.advance(9)
    assert breaker.is_open
    clock.advance(1)
    assert breaker.call(lambda: "back") == "back"


def test_circuit_breaker_rejects_invalid_configuration():
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=0)
    with pytest.raises(ValueError):
        retry_with_backoff(lambda: 1, attempts=0)


# ---------------------------------------------------------------------------
# Scheduling facts are independent of the provider
# ---------------------------------------------------------------------------


def _control_result():
    return MultiAgentOptimizer(llm=NoopLLM()).optimize(PROJECT)


def _assert_same_schedule(left, right) -> None:
    assert left.project_duration == right.project_duration
    assert left.critical_path == right.critical_path
    assert left.topological_order == right.topological_order
    for task in left.tasks:
        assert vars(left.tasks[task]) == vars(right.tasks[task]), task


@pytest.mark.parametrize(
    "llm_factory,name",
    [
        (lambda: FailingLLM(_timeout), "timeout"),
        (lambda: FailingLLM(_rate_limit), "rate_limit"),
        (lambda: FailingLLM(_connection_reset), "repeated_failure"),
        (lambda: StaticLLM("not json"), "invalid_output"),
        (lambda: StaticLLM('{"notes": null, "note": null, "thought": null}'), "wrong_schema"),
    ],
)
def test_provider_failures_cannot_alter_scheduling_facts(llm_factory, name):
    control = _control_result()
    result = MultiAgentOptimizer(llm=llm_factory()).optimize(PROJECT)

    _assert_same_schedule(result.schedule, control.schedule)
    assert result.schedule.project_duration == EXPECTED_DURATION, name
    assert result.schedule.critical_path == EXPECTED_CRITICAL, name

    # Deterministic enrichment and the narrative fallback are unchanged.
    assert result.narrative == control.narrative, name
    assert result.risks == control.risks, name
    assert result.suggestions == control.suggestions, name
    assert all(risk.deterministic for risk in result.risks), name
    assert all(suggestion.deterministic for suggestion in result.suggestions), name


def test_failures_do_not_mutate_the_input_project():
    project = {
        "A": {"duration": 3},
        "B": {"duration": 2, "dependencies": ["A"]},
    }
    snapshot = {task: dict(body) for task, body in project.items()}
    MultiAgentOptimizer(llm=FailingLLM(_timeout)).optimize(project)
    assert project == snapshot


def test_invalid_project_is_rejected_before_any_provider_call():
    llm = FailingLLM(_timeout)
    with pytest.raises(ProjectError):
        MultiAgentOptimizer(llm=llm).optimize({"A": {"duration": -1}})
    assert llm.calls == 0


# ---------------------------------------------------------------------------
# Circuit opening and recovery across repeated optimizer runs
# ---------------------------------------------------------------------------


def _agents(optimizer):
    return (optimizer.scheduler, optimizer.risk_agent, optimizer.optimiser)


def test_repeated_failure_opens_each_agents_circuit_then_recovers():
    clock = FakeClock()
    llm = ToggleLLM(_timeout)
    optimizer = MultiAgentOptimizer(llm=llm)
    for agent in _agents(optimizer):
        agent._breaker = CircuitBreaker(
            failure_threshold=3, reset_seconds=30, clock=clock
        )

    control = _control_result()
    for run in range(3):
        result = optimizer.optimize(PROJECT)
        _assert_same_schedule(result.schedule, control.schedule)
        assert all(agent._breaker.failure_count == run + 1 for agent in _agents(optimizer))

    assert all(agent._breaker.is_open for agent in _agents(optimizer))
    calls_when_open = llm.calls
    assert calls_when_open == 3 * 3 * 2  # runs x agents x retry attempts

    # While open, no provider call is made; the documented fallback still works.
    blocked = optimizer.optimize(PROJECT)
    assert llm.calls == calls_when_open
    _assert_same_schedule(blocked.schedule, control.schedule)
    assert blocked.narrative == control.narrative
    assert blocked.risks == control.risks
    assert blocked.suggestions == control.suggestions

    # Recovery is observable: after the reset window the next call probes the
    # provider, and a healthy provider restores LLM enrichment.
    clock.advance(30)
    llm.healthy = True
    recovered = optimizer.optimize(PROJECT)
    assert llm.calls > calls_when_open
    _assert_same_schedule(recovered.schedule, control.schedule)
    assert any(not risk.deterministic for risk in recovered.risks)
    assert any(not suggestion.deterministic for suggestion in recovered.suggestions)
    assert recovered.narrative == "recovered"  # enrichment is back
    assert all(not agent._breaker.is_open for agent in _agents(optimizer))
    assert all(agent._breaker.failure_count == 0 for agent in _agents(optimizer))


def test_scheduler_falls_back_to_the_same_narrative_table():
    control = _control_result()
    failed = MultiAgentOptimizer(llm=FailingLLM(_rate_limit)).optimize(PROJECT)
    assert failed.narrative == control.narrative
    assert "CRITICAL" in failed.narrative


# ---------------------------------------------------------------------------
# Core vs optional graph path
# ---------------------------------------------------------------------------


def test_graph_path_matches_core_outputs_when_deterministic():
    pytest.importorskip("langgraph")
    from multi_agent_optimizer.lg_graph import run_langgraph

    core = MultiAgentOptimizer(llm=NoopLLM()).optimize(PROJECT)
    state = run_langgraph(PROJECT)

    assert state["duration"] == core.schedule.project_duration
    assert state["critical_path"] == core.schedule.critical_path
    assert state["narrative"] == core.narrative
    assert state["risks"] == [risk.model_dump() for risk in core.risks]
    assert state["suggestions"] == [item.model_dump() for item in core.suggestions]


def test_graph_path_preserves_scheduling_facts_under_provider_failure():
    pytest.importorskip("langgraph")
    from multi_agent_optimizer.lg_graph import run_langgraph

    core = MultiAgentOptimizer(llm=NoopLLM()).optimize(PROJECT)
    state = run_langgraph(PROJECT, llm=FailingLLM(_timeout))

    assert state["duration"] == EXPECTED_DURATION
    assert state["critical_path"] == EXPECTED_CRITICAL
    assert state["narrative"] == core.narrative
    assert state["risks"] == [risk.model_dump() for risk in core.risks]
    assert state["suggestions"] == [item.model_dump() for item in core.suggestions]
    assert not any(risk.get("deterministic") is False for risk in state["risks"])
    assert not any(item.get("deterministic") is False for item in state["suggestions"])
