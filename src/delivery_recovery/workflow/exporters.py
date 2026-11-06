"""Disabled-by-default LangSmith export seam.

The local ``TraceRecorder`` remains the source of truth for offline evidence.
This module defines the seam that can forward the same parent/child spans to a
LangSmith sink when credentials and trace-export scope are established. Tests
use a recording sink; no network client is constructed or called here.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Protocol

__all__ = ["LangSmithClientSink", "LangSmithRunSink", "LangSmithTraceExporter"]

_SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "access_token",
    "token",
}


class LangSmithRunSink(Protocol):
    def start_run(
        self,
        *,
        name: str,
        run_type: str,
        parent_run_id: str | None,
        metadata: dict[str, Any],
        attributes: dict[str, Any],
        start_time: float,
        trace_id: str,
    ) -> str: ...

    def end_run(
        self,
        *,
        run_id: str,
        status: str,
        end_time: float | None,
        error_type: str | None,
        outputs: dict[str, Any] | None = None,
    ) -> None: ...


_RUN_TYPES = {
    "node": "chain",
    "provider": "llm",
    "tool": "tool",
    "validation": "chain",
    "fallback": "chain",
}


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return datetime.now(timezone.utc)


class LangSmithClientSink:
    """Concrete sink for a LangSmith-compatible client.

    The client is injected, so the default path remains offline and testable.
    The caller is responsible for constructing an authenticated client only
    after access and trace-export scope are established.
    """

    def __init__(self, client: Any, *, project_name: str | None = None) -> None:
        self.client = client
        self.project_name = project_name
        self._orders: dict[str, tuple[str, str]] = {}

    def start_run(
        self,
        *,
        name: str,
        run_type: str,
        parent_run_id: str | None,
        metadata: dict[str, Any],
        attributes: dict[str, Any],
        start_time: float | str | datetime,
        trace_id: str,
    ) -> str:
        run_id = uuid.uuid4()
        start_dt = _as_datetime(start_time)
        current_order = (
            start_dt.strftime("%Y%m%dT%H%M%S%fZ") + str(run_id)
        )
        if parent_run_id and parent_run_id in self._orders:
            parent_trace_id, parent_order = self._orders[parent_run_id]
            trace = parent_trace_id
            dotted_order = parent_order + "." + current_order
        else:
            # LangSmith requires trace_id to be the root run id encoded in the
            # first dotted-order segment. Derive it from this run, never from
            # an unrelated local trace identifier.
            trace = str(run_id)
            dotted_order = current_order

        self.client.create_run(
            id=run_id,
            trace_id=trace,
            dotted_order=dotted_order,
            name=name,
            run_type=_RUN_TYPES.get(run_type, "chain"),
            inputs={},
            start_time=start_dt,
            parent_run_id=parent_run_id,
            project_name=self.project_name,
            extra={"metadata": metadata, "attributes": attributes},
        )
        self._orders[str(run_id)] = (trace, dotted_order)
        return str(run_id)

    def end_run(
        self,
        *,
        run_id: str,
        status: str,
        end_time: float | str | datetime | None,
        error_type: str | None,
        outputs: dict[str, Any] | None = None,
    ) -> None:
        self.client.update_run(
            run_id,
            end_time=_as_datetime(end_time),
            error=error_type,
            outputs=_sanitize(outputs or {}),
        )


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize(item)
            for key, item in value.items()
            if key.lower() not in _SECRET_KEYS
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


#: Metadata keys the LangSmith SDK reads for native model and token attribution.
#: ``usage_metadata`` is consumed from ``extra.metadata`` (or ``outputs``) by
#: ``langsmith.run_helpers._extract_usage``.
_NATIVE_METADATA_KEYS = ("ls_provider", "ls_model_name", "usage_metadata")


def _span_metadata(
    metadata: dict[str, Any],
    *,
    is_root: bool,
    kind: str,
    attributes: dict[str, Any],
) -> dict[str, Any]:
    if is_root:
        return metadata
    base = {
        key: value
        for key, value in metadata.items()
        if key not in _NATIVE_METADATA_KEYS
    }
    if kind == "provider":
        for key in ("ls_provider", "ls_model_name"):
            if key in attributes:
                base[key] = attributes[key]
            elif metadata.get(key) is not None:
                base[key] = metadata[key]
        if "usage_metadata" in attributes:
            base["usage_metadata"] = attributes["usage_metadata"]
    return base


def _safe_outputs(
    span: dict[str, Any],
    *,
    is_root: bool,
    safe_summary: Any,
) -> dict[str, Any]:
    if is_root and isinstance(safe_summary, dict):
        return _sanitize(safe_summary)
    attributes = span.get("attributes") or {}
    if span.get("kind") == "tool" and isinstance(
        attributes.get("result_summary"), dict
    ):
        return {"result_summary": _sanitize(attributes["result_summary"])}
    return {}


class LangSmithTraceExporter:
    def __init__(self, *, sink: LangSmithRunSink | None = None, enabled: bool = False) -> None:
        self.sink = sink
        self.enabled = enabled

    def export(self, trace: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            return {"exported": False, "reason": "disabled"}
        if self.sink is None:
            return {"exported": False, "reason": "no_sink"}

        metadata = _sanitize(trace.get("metadata") or {})
        workflow_status = str(metadata.get("workflow_status") or "unknown")
        reason_code = metadata.get("reason_code")
        safe_summary = metadata.get("safe_summary")
        run_ids: dict[str, str] = {}
        spans = trace.get("spans") or []
        try:
            roots = [span for span in spans if not span.get("parent_id")]
            single_root = len(roots) == 1
            synthetic_root_id = None
            if spans and not single_root:
                start_times = [span.get("started_at_utc") for span in spans if span.get("started_at_utc")]
                end_times = [span.get("ended_at_utc") for span in spans if span.get("ended_at_utc")]
                synthetic_root_id = self.sink.start_run(
                    name="workflow",
                    run_type="chain",
                    parent_run_id=None,
                    metadata=metadata,
                    attributes={
                        "synthetic_root": True,
                        "workflow_status": workflow_status,
                        "reason_code": reason_code,
                    },
                    start_time=start_times[0] if start_times else 0.0,
                    trace_id=str(trace.get("trace_id") or ""),
                )
                run_ids["__root__"] = synthetic_root_id

            for span in spans:
                parent_id = span.get("parent_id")
                parent_run_id = (
                    run_ids.get(parent_id)
                    if parent_id
                    else synthetic_root_id
                )
                attributes = _sanitize(span.get("attributes") or {})
                is_root_span = single_root and not parent_id
                run_id = self.sink.start_run(
                    name=span["name"],
                    run_type=span["kind"],
                    parent_run_id=parent_run_id,
                    metadata=_span_metadata(
                        metadata,
                        is_root=is_root_span,
                        kind=span["kind"],
                        attributes=attributes,
                    ),
                    attributes=attributes,
                    start_time=span.get("started_at_utc")
                    or float(span.get("started_at") or 0.0),
                    trace_id=str(trace.get("trace_id") or ""),
                )
                run_ids[span["span_id"]] = run_id or span["span_id"]
            for span in spans:
                if span.get("name") == "workflow" and workflow_status != "unknown":
                    status = workflow_status
                    error_type = reason_code if workflow_status == "failed" else None
                else:
                    status = span.get("status", "unknown")
                    attributes = span.get("attributes") or {}
                    error_type = attributes.get("error_type") or attributes.get(
                        "error_code"
                    )
                is_root_span = single_root and not span.get("parent_id")
                self.sink.end_run(
                    run_id=run_ids[span["span_id"]],
                    status=status,
                    end_time=span.get("ended_at_utc") or span.get("ended_at"),
                    error_type=error_type,
                    outputs=_safe_outputs(
                        span,
                        is_root=is_root_span,
                        safe_summary=safe_summary,
                    ),
                )
            if synthetic_root_id is not None:
                self.sink.end_run(
                    run_id=synthetic_root_id,
                    status=workflow_status,
                    end_time=max(end_times) if end_times else None,
                    error_type=reason_code if workflow_status == "failed" else None,
                    outputs=(
                        _sanitize(safe_summary)
                        if isinstance(safe_summary, dict)
                        else {}
                    ),
                )
        except Exception as exc:
            return {
                "exported": False,
                "reason": "sink_error",
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:300],
            }
        return {"exported": True, "run_ids": run_ids}
