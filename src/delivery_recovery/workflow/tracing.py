"""Small local span recorder for Phase 2 evidence.

This is deliberately deterministic and offline. It records parent/child spans
for graph nodes, tools, provider calls, validation and fallback. A LangSmith
exporter can be attached later when workspace access is established; no
credentials or sensitive message content are written into spans.
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

__all__ = ["Span", "TraceRecorder"]


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_id: str | None
    name: str
    kind: str
    started_at: float
    started_at_utc: datetime
    ended_at: float | None = None
    ended_at_utc: datetime | None = None
    status: str = "running"
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "kind": self.kind,
            "started_at": round(self.started_at, 6),
            "started_at_utc": self.started_at_utc.isoformat(),
            "ended_at": round(self.ended_at, 6) if self.ended_at is not None else None,
            "ended_at_utc": (
                self.ended_at_utc.isoformat() if self.ended_at_utc is not None else None
            ),
            "status": self.status,
            "attributes": self.attributes,
        }


class TraceRecorder:
    def __init__(
        self,
        run_id: str,
        *,
        metadata: dict[str, Any] | None = None,
        clock=time.perf_counter,
    ) -> None:
        self.run_id = run_id
        self.trace_id = run_id
        self.metadata = metadata or {}
        self._clock = clock
        self._spans: list[Span] = []
        self._stack: list[Span] = []

    @contextmanager
    def span(self, name: str, kind: str, **attributes: Any) -> Iterator[Span]:
        parent = self._stack[-1] if self._stack else None
        span = Span(
            trace_id=self.trace_id,
            span_id=uuid.uuid4().hex,
            parent_id=parent.span_id if parent else None,
            name=name,
            kind=kind,
            started_at=self._clock(),
            started_at_utc=datetime.now(timezone.utc),
            attributes=dict(attributes),
        )
        self._spans.append(span)
        self._stack.append(span)
        try:
            yield span
            if span.status == "running":
                span.status = "ok"
        except BaseException as exc:
            span.status = "error"
            span.attributes["error_type"] = type(exc).__name__
            raise
        finally:
            span.ended_at = self._clock()
            span.ended_at_utc = datetime.now(timezone.utc)
            self._stack.pop()

    def finalize(
        self,
        status: str,
        *,
        reason_code: str | None = None,
        safe_summary: dict[str, Any] | None = None,
    ) -> None:
        self.metadata["workflow_status"] = status
        self.metadata["reason_code"] = reason_code
        if safe_summary is not None:
            self.metadata["safe_summary"] = safe_summary
        for span in self._spans:
            if span.name == "workflow" and span.parent_id is None:
                span.attributes["workflow_status"] = status
                span.attributes["reason_code"] = reason_code
                if status == "failed":
                    span.status = "error"
                break

    def export(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "metadata": dict(self.metadata),
            "spans": [span.to_dict() for span in self._spans],
        }
