"""Phase 3 local application runtime: storage, checkpoints, identity and worker."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from ..approvals import DecisionService
from ..storage.config import StorageConfig
from ..storage.store import ApplicationStore
from ..workflow.demo_provider import LocalDemoProvider
from ..workflow.service import RecoveryWorkflow
from ..workflow.worker import JobWorker
from .identity import IdentityProvider, load_or_create_demo_identities

__all__ = ["Phase3Runtime", "build_runtime"]


@dataclass
class Phase3Runtime:
    config: StorageConfig
    store: ApplicationStore
    identity: IdentityProvider
    checkpointer: SqliteSaver
    checkpoint_connection: sqlite3.Connection
    workflow: RecoveryWorkflow
    worker: JobWorker
    decisions: DecisionService
    provider: Any
    source_revision: str = "unknown"

    def ready(self) -> dict[str, Any]:
        storage_ok = self.store.ready()
        worker_ok = self.store.worker_ready(self.worker.worker_id, max_age_seconds=15)
        status = "ready" if storage_ok and worker_ok else "not_ready"
        return {
            "status": status,
            "mode": "phase3",
            "storage": "ok" if storage_ok else "unavailable",
            "schema_version": self.store.schema_version,
            "worker": "running" if worker_ok else "unavailable",
            "queue_depth": self.store.queue_depth(),
            "pending_human": self.store.pending_human_count(),
            "model": "local-demo",
        }

    def close(self) -> None:
        self.worker.stop()
        try:
            self.checkpoint_connection.close()
        except Exception:
            pass


def build_runtime(
    config: StorageConfig | None = None,
    *,
    provider: Any | None = None,
    start_worker: bool = True,
    source_revision: str = "unknown",
) -> Phase3Runtime:
    config = (config or StorageConfig.from_env()).ensure()
    store = ApplicationStore(config)
    identity = load_or_create_demo_identities(config.data_dir)
    checkpoint_connection = sqlite3.connect(
        str(config.checkpoint_path), check_same_thread=False
    )
    checkpointer = SqliteSaver(checkpoint_connection)
    provider = provider or LocalDemoProvider()
    workflow = RecoveryWorkflow(
        provider,
        checkpointer=checkpointer,
        source_revision=source_revision,
        require_approval=True,
        decision_resolver=store.get_decision,
    )
    worker = JobWorker(store, workflow, config=config)
    runtime = Phase3Runtime(
        config=config,
        store=store,
        identity=identity,
        checkpointer=checkpointer,
        checkpoint_connection=checkpoint_connection,
        workflow=workflow,
        worker=worker,
        decisions=DecisionService(store),
        provider=provider,
        source_revision=source_revision,
    )
    if start_worker:
        worker.start()
    return runtime
