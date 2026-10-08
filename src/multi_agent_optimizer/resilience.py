"""Resilience: retry-with-backoff, a circuit breaker, and validated LLM calls.

Everything here is deterministic in behaviour: when an LLM provider is
unavailable or returns something that fails Pydantic validation, callers get a
documented fallback instead of a crash.
"""

from __future__ import annotations

import json
import time
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError

__all__ = [
    "CircuitOpenError",
    "CircuitBreaker",
    "retry_with_backoff",
    "extract_json_object",
    "parse_validated",
    "safe_llm_json",
]

T = TypeVar("T", bound=BaseModel)


class CircuitOpenError(RuntimeError):
    """Raised when the circuit breaker is open."""


class CircuitBreaker:
    """Trips open after ``failure_threshold`` consecutive failures, then stays
    open for ``reset_seconds``."""

    def __init__(self, failure_threshold: int = 3, reset_seconds: float = 30.0):
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self._threshold = failure_threshold
        self._reset = reset_seconds
        self._failures = 0
        self._open_until = 0.0

    @property
    def is_open(self) -> bool:
        return time.monotonic() < self._open_until

    def call(self, fn: Callable[[], T]) -> T:
        if self.is_open:
            raise CircuitOpenError("circuit open; not calling through")
        try:
            result = fn()
        except Exception:
            self._failures += 1
            if self._failures >= self._threshold:
                self._open_until = time.monotonic() + self._reset
            raise
        self._failures = 0
        return result


def retry_with_backoff(
    fn: Callable[[], T],
    attempts: int = 3,
    base_delay: float = 0.1,
    factor: float = 2.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> T:
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except exceptions as exc:  # type: ignore[misc]
            last = exc
            if i + 1 < attempts:
                time.sleep(base_delay * (factor**i))
    assert last is not None
    raise last


def extract_json_object(text: str) -> str:
    """Pull the first balanced { ... } block out of arbitrary LLM text."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object found in LLM output")
    return text[start : end + 1]


def parse_validated(llm, schema: type[T], system: str, user: str) -> T:
    """Call the LLM and parse/validate JSON into ``schema``.

    Raises ``ValidationError``/``ValueError`` on bad output — the caller
    decides the fallback (see :func:`safe_llm_json`).
    """
    raw = llm.chat(system, user)
    return schema.model_validate_json(extract_json_object(raw))


def safe_llm_json(
    llm,
    schema: type[T],
    system: str,
    user: str,
    *,
    fallback: T,
    breaker: CircuitBreaker,
) -> T:
    """Circuit-broken, retried, Pydantic-validated LLM call with a deterministic
    fallback when anything fails (provider error, parsed-JSON invalid, breaker open)."""
    try:
        return breaker.call(
            lambda: retry_with_backoff(
                lambda: parse_validated(llm, schema, system, user),
                attempts=2,
                exceptions=(Exception,),
            )
        )
    except (Exception, ValidationError):  # noqa: BLE001 - intentional fallback
        return fallback