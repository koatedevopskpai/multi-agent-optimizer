"""Single-process durable job worker for the local reference."""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any

from ..storage.config import StorageConfig
from ..storage.store import ApplicationStore
from .contracts import DraftRecoveryRequest
from .service import RecoveryWorkflow

__all__ = ["JobWorker"]

logger = logging.getLogger(__name__)


class JobWorker:
    def __init__(
        self,
        store: ApplicationStore,
        workflow: RecoveryWorkflow,
        *,
        config: StorageConfig,
        worker_id: str | None = None,
        poll_seconds: float = 0.2,
    ) -> None:
        self.store = store
        self.workflow = workflow
        self.config = config
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self.store.worker_heartbeat(self.worker_id)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="delivery-recovery-worker", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
                self.store.worker_heartbeat(self.worker_id)
            except Exception:
                logger.exception("worker loop failed")
                self.last_error = "worker_loop_failed"
            self._stop.wait(self.poll_seconds)

    def run_once(self) -> dict[str, Any] | None:
        claimed = self.store.claim_next(self.worker_id)
        if claimed is None:
            return None
        run = claimed["run"]
        job = claimed["job"]
        run_id = run["run_id"]
        thread_id = run["thread_id"]
        try:
            resume_kind = job.get("resume_kind")
            payload = job.get("resume_payload") or {}
            if resume_kind == "clarification":
                result = self.workflow.resume(thread_id, payload.get("answer"))
            elif resume_kind == "decision":
                result = self.workflow.resume(
                    thread_id, {"decision_id": payload.get("decision_id")}
                )
            else:
                draft = DraftRecoveryRequest.model_validate(run["request"])
                result = self.workflow.start(
                    draft, run_id=run_id, thread_id=thread_id
                )
            self._persist(result, run_id)
            return {"run_id": run_id, "status": result.status}
        except Exception as exc:
            logger.exception("job failed: %s", run_id)
            self.last_error = type(exc).__name__
            try:
                self.store.fail_run(
                    run_id,
                    code="WORKER_FAILURE",
                    message=f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                logger.exception("failed to persist worker failure")
            return {"run_id": run_id, "status": "failed"}

    def _persist(self, result, run_id: str) -> None:
        base = {
            "evaluation": (
                result.evaluation.model_dump(mode="json")
                if result.evaluation is not None
                else None
            ),
            "explanation": (
                result.explanation.model_dump(mode="json")
                if result.explanation is not None
                else None
            ),
            "provider_outcome": (
                result.provider_outcome.model_dump(mode="json")
                if result.provider_outcome is not None
                else None
            ),
        }
        self.store.update_result(run_id, **base)
        if result.status == "needs_input":
            self.store.mark_waiting(
                run_id,
                status="needs_input",
                pending=(
                    result.clarification.model_dump(mode="json")
                    if result.clarification is not None
                    else None
                ),
            )
            return
        if result.status == "awaiting_approval":
            self.store.mark_waiting(
                run_id,
                status="awaiting_approval",
                pending=(
                    result.pending_decision.model_dump(mode="json")
                    if result.pending_decision is not None
                    else None
                ),
            )
            return
        if result.status in {"completed", "degraded", "rejected"}:
            self.store.complete_run(
                run_id,
                status=result.status,
                **base,
            )
            return
        if result.status == "failed":
            self.store.fail_run(
                run_id,
                code=result.errors[0].code if result.errors else "WORKFLOW_FAILED",
                message=(
                    result.errors[0].message
                    if result.errors
                    else "The workflow failed."
                ),
            )
            return
        self.store.fail_run(
            run_id,
            code="WORKFLOW_STATUS_UNKNOWN",
            message=f"Unhandled workflow status {result.status!r}.",
        )
