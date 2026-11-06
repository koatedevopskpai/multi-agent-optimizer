"""Shared Phase 3 test helpers (not a test module)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from delivery_recovery.api.runtime import Phase3Runtime, build_runtime
from delivery_recovery.storage.config import StorageConfig


def _phase2_helpers():
    path = Path(__file__).with_name("test_phase2_workflow.py")
    spec = importlib.util.spec_from_file_location("phase2_helpers", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


HELPERS = _phase2_helpers()


def draft(**overrides):
    return HELPERS._draft(**overrides)


def final_response():
    return HELPERS._final_response()


def build_test_runtime(tmp_path: Path, *, start_worker: bool = False) -> Phase3Runtime:
    config = StorageConfig(
        data_dir=tmp_path,
        db_path=tmp_path / "application.db",
        checkpoint_path=tmp_path / "checkpoints.db",
        lease_seconds=30,
        pending_retention_seconds=3600,
    )
    return build_runtime(config, start_worker=start_worker)


def tokens(runtime: Phase3Runtime) -> dict[str, str]:
    return {
        identity.principal.actor_id: identity.token
        for identity in runtime.identity.identities
    }


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
