"""Provider boundary with deterministic fakes and bounded failure handling.

The workflow never talks to a real provider by default. ``FakeProvider`` makes
auth, throttle, timeout, transport and schema behaviour reproducible without
network access. ``ProviderRunner`` classifies failures, bounds transport
attempts and owns a process-scoped circuit breaker.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CallOutcome",
    "ChatMessage",
    "FakeClock",
    "FakeProvider",
    "ProviderAdmissionError",
    "ProviderAuthError",
    "ProviderBudgetError",
    "ProviderError",
    "ProviderLimits",
    "ProviderRequest",
    "ProviderResponse",
    "ProviderRunner",
    "ProviderSchemaError",
    "ProviderThrottleError",
    "ProviderTimeoutError",
    "ProviderTransportError",
    "ToolCall",
    "TransportAttempt",
]


class ProviderError(Exception):
    classification = "provider"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ProviderAuthError(ProviderError):
    classification = "auth"


class ProviderThrottleError(ProviderError):
    classification = "throttle"


class ProviderTimeoutError(ProviderError):
    classification = "timeout"


class ProviderTransportError(ProviderError):
    classification = "transport"


class ProviderSchemaError(ProviderError):
    classification = "schema"


class ProviderBudgetError(ProviderError):
    classification = "budget"


class ProviderAdmissionError(ProviderBudgetError):
    """A budget/session guard rejected the request before transport started.

    This is deliberately distinct from a provider ``ProviderBudgetError`` raised
    after an admitted transport begins; only wrapper admission boundaries should
    raise it.
    """

    classification = "admission"


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = ""
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None


class ProviderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    usage_reported: bool = True


class ProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    messages: list[ChatMessage]
    tools: list[dict[str, Any]] = Field(default_factory=list)
    max_output_tokens: int = 1_000
    run_id: str
    deadline_at: float | None = None
    remaining_seconds: float | None = None


class ProviderLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_transport_calls: int = Field(default=2, ge=1, le=6)
    max_elapsed_seconds: float = Field(default=5.0, gt=0)
    breaker_threshold: int = Field(default=3, ge=1)
    breaker_reset_seconds: float = Field(default=30.0, gt=0)


@dataclass(frozen=True)
class TransportAttempt:
    """One actual admission into the provider transport.

    Attempts rejected before admission (open circuit, deadline, exhausted
    transport budget or token-capacity guard) are not represented here.
    """

    number: int
    ok: bool
    response: ProviderResponse | None = None
    reason_code: str | None = None
    classification: str | None = None


@dataclass
class CallOutcome:
    ok: bool
    response: ProviderResponse | None
    reason_code: str | None
    transport_attempts: int
    elapsed_ms: int
    provider: str
    model: str
    logical_attempts: int = 1
    attempts: list[TransportAttempt] = field(default_factory=list)


class ModelProvider(Protocol):
    name: str
    model: str

    def complete(self, request: ProviderRequest) -> ProviderResponse: ...


class FakeProvider:
    """Deterministic provider driven by a finite response/exception script."""

    name = "fake"
    model = "fake-v1"

    def __init__(self, script: Sequence[ProviderResponse | Exception]) -> None:
        self._script = list(script)
        self.calls: list[ProviderRequest] = []

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.calls.append(request)
        if not self._script:
            raise ProviderTransportError("fake provider has no response left")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class _Breaker:
    threshold: int
    reset_seconds: float
    failures: int = 0
    open_until: float = 0.0

    def allow(self, now: float) -> bool:
        return now >= self.open_until

    def success(self) -> None:
        self.failures = 0
        self.open_until = 0.0

    def failure(self, now: float) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.open_until = now + self.reset_seconds


_TRANSIENT = (ProviderThrottleError, ProviderTimeoutError, ProviderTransportError)
_REASON = {
    "auth": "PROVIDER_AUTH",
    "throttle": "PROVIDER_THROTTLE",
    "timeout": "PROVIDER_TIMEOUT",
    "transport": "PROVIDER_TRANSPORT",
    "schema": "INVALID_MODEL_OUTPUT",
    "budget": "BUDGET_EXCEEDED",
    "admission": "BUDGET_EXCEEDED",
}


class ProviderRunner:
    """Bound transport attempts and own a process-scoped breaker."""

    def __init__(
        self,
        provider: ModelProvider,
        *,
        limits: ProviderLimits | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.provider = provider
        self.limits = limits or ProviderLimits()
        self.clock = clock or time.monotonic
        self._breaker = _Breaker(
            threshold=self.limits.breaker_threshold,
            reset_seconds=self.limits.breaker_reset_seconds,
        )

    def complete(
        self,
        request: ProviderRequest,
        *,
        max_transport_calls: int | None = None,
        before_attempt: Callable[[], str | None] | None = None,
    ) -> CallOutcome:
        """Run bounded transport attempts and return a per-attempt ledger.

        ``before_attempt`` is invoked immediately before each actual transport
        admission. Returning a reason code rejects the attempt without counting
        it; returning ``None`` admits it. This lets the caller reserve token
        capacity per attempt rather than per logical call.
        """

        started = self.clock()
        attempts = 0
        ledger: list[TransportAttempt] = []
        limit = (
            self.limits.max_transport_calls
            if max_transport_calls is None
            else min(self.limits.max_transport_calls, max(0, max_transport_calls))
        )
        if limit <= 0:
            return self._fail("EXECUTION_LIMIT", 0, started, ledger)

        while attempts < limit:
            now = self.clock()
            if not self._breaker.allow(now):
                return self._fail("CIRCUIT_OPEN", attempts, started, ledger)

            remaining_candidates: list[float] = []
            if request.deadline_at is not None:
                remaining_candidates.append(request.deadline_at - now)
            if request.remaining_seconds is not None:
                # The request value is the allowance at the start of this
                # runner call; consume it across retries.
                remaining_candidates.append(
                    request.remaining_seconds - (now - started)
                )
            remaining_candidates.append(
                self.limits.max_elapsed_seconds - (now - started)
            )
            remaining = min(remaining_candidates)
            if remaining <= 0:
                return self._fail("DEADLINE_EXCEEDED", attempts, started, ledger)

            if before_attempt is not None:
                rejection = before_attempt()
                if rejection:
                    return self._fail(rejection, attempts, started, ledger)

            attempts += 1
            attempt_request = request.model_copy(update={"remaining_seconds": remaining})
            try:
                response = self.provider.complete(attempt_request)
                if not isinstance(response, ProviderResponse):
                    raise ProviderSchemaError("provider returned a non-ProviderResponse value")
                self._breaker.success()
                ledger.append(
                    TransportAttempt(number=attempts, ok=True, response=response)
                )
                return CallOutcome(
                    ok=True,
                    response=response,
                    reason_code=None,
                    transport_attempts=attempts,
                    elapsed_ms=int((self.clock() - started) * 1000),
                    provider=self.provider.name,
                    model=self.provider.model,
                    attempts=ledger,
                )
            except ProviderAuthError as exc:
                ledger.append(
                    TransportAttempt(
                        number=attempts,
                        ok=False,
                        reason_code=_REASON["auth"],
                        classification=exc.classification,
                    )
                )
                return self._fail(_REASON["auth"], attempts, started, ledger)
            except ProviderAdmissionError:
                # The wrapped provider never started its transport. Release the
                # provisional admission by not adding a ledger entry and not
                # advancing the transport count.
                return self._fail(
                    _REASON["admission"],
                    max(0, attempts - 1),
                    started,
                    ledger,
                )
            except ProviderBudgetError as exc:
                ledger.append(
                    TransportAttempt(
                        number=attempts,
                        ok=False,
                        reason_code=_REASON["budget"],
                        classification=exc.classification,
                    )
                )
                return self._fail(_REASON["budget"], attempts, started, ledger)
            except ProviderSchemaError as exc:
                ledger.append(
                    TransportAttempt(
                        number=attempts,
                        ok=False,
                        reason_code=_REASON["schema"],
                        classification=exc.classification,
                    )
                )
                return self._fail(_REASON["schema"], attempts, started, ledger)
            except _TRANSIENT as exc:
                reason = _REASON[exc.classification]
                ledger.append(
                    TransportAttempt(
                        number=attempts,
                        ok=False,
                        reason_code=reason,
                        classification=exc.classification,
                    )
                )
                self._breaker.failure(now)
                if attempts >= limit:
                    return self._fail(reason, attempts, started, ledger)

        return self._fail("PROVIDER_TRANSPORT", attempts, started, ledger)

    def _fail(
        self,
        reason: str,
        attempts: int,
        started: float,
        ledger: list[TransportAttempt] | None = None,
    ) -> CallOutcome:
        return CallOutcome(
            ok=False,
            response=None,
            reason_code=reason,
            transport_attempts=attempts,
            elapsed_ms=int((self.clock() - started) * 1000),
            provider=self.provider.name,
            model=self.provider.model,
            attempts=list(ledger or []),
        )
