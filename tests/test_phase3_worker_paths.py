"""Worker lifecycle and failure-path tests."""

from __future__ import annotations

import time
from types import SimpleNamespace

from delivery_recovery.api.runtime import build_runtime
from delivery_recovery.storage.config import StorageConfig
from delivery_recovery.workflow.contracts import WorkflowError

from phase3_support import draft


def _runtime(tmp_path, *, provider=None):
    config = StorageConfig(
        data_dir=tmp_path,
        db_path=tmp_path / "app.db",
        checkpoint_path=tmp_path / "cp.db",
        lease_seconds=30,
    )
    return build_runtime(config, provider=provider, start_worker=False)


def _create(store, run_id="run-worker"):
    value = draft()
    return store.create_run(
        draft_json=value.model_dump(mode="json"),
        request_hash="hash",
        run_id=run_id,
        thread_id=run_id,
        principal_id="planner.demo",
        idempotency_key=f"key-{run_id}",
    )


def test_worker_start_is_idempotent_and_loop_records_failure(tmp_path):
    runtime = _runtime(tmp_path)
    try:
        runtime.worker.start()
        runtime.worker.start()  # second start returns without another thread
        assert runtime.worker.running is True
        runtime.worker.stop()

        def boom():
            raise RuntimeError("loop failure")

        runtime.worker.run_once = boom  # type: ignore[method-assign]
        runtime.worker.start()
        time.sleep(0.05)
        runtime.worker.stop()
        assert runtime.worker.last_error == "worker_loop_failed"
    finally:
        runtime.close()


def test_worker_failed_result_and_unknown_status_persist(tmp_path):
    runtime = _runtime(tmp_path)
    try:
        _create(runtime.store, "run-failed-result")
        result = SimpleNamespace(
            status="failed",
            evaluation=None,
            explanation=None,
            provider_outcome=None,
            errors=[WorkflowError(code="SYNTHETIC", message="workflow failed")],
        )
        runtime.worker._persist(result, "run-failed-result")
        record = runtime.store.get_run("run-failed-result")
        assert record["status"] == "failed"
        assert record["error"]["code"] == "SYNTHETIC"

        _create(runtime.store, "run-unknown")
        runtime.worker._persist(
            SimpleNamespace(
                status="mystery",
                evaluation=None,
                explanation=None,
                provider_outcome=None,
                errors=[],
            ),
            "run-unknown",
        )
        assert runtime.store.get_run("run-unknown")["error"]["code"] == (
            "WORKFLOW_STATUS_UNKNOWN"
        )
    finally:
        runtime.close()


def test_worker_exception_marks_job_failed(tmp_path):
    class BrokenProvider:
        name = "broken"
        model = "broken"

        def complete(self, request):
            raise RuntimeError("unexpected programming error")

    runtime = _runtime(tmp_path, provider=BrokenProvider())
    try:
        _create(runtime.store, "run-broken")
        result = runtime.worker.run_once()
        assert result["status"] == "failed"
        record = runtime.store.get_run("run-broken")
        assert record["status"] == "failed"
        assert record["error"]["code"] == "WORKER_FAILURE"
    finally:
        runtime.close()
