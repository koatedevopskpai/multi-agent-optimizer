"""Explicit local storage configuration.

The reference is deliberately single-instance: one application process, one
worker and two SQLite files (application transactions and graph checkpoints).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

__all__ = ["StorageConfig"]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class StorageConfig:
    data_dir: Path
    db_path: Path
    checkpoint_path: Path
    max_queued_jobs: int = 4
    max_pending_human: int = 100
    lease_seconds: int = 30
    pending_retention_seconds: int = 86_400

    @classmethod
    def from_env(cls) -> "StorageConfig":
        data_dir = Path(
            os.environ.get(
                "DELIVERY_RECOVERY_DATA_DIR",
                str(Path.cwd() / "var" / "delivery-recovery"),
            )
        ).resolve()
        return cls(
            data_dir=data_dir,
            db_path=data_dir / "application.db",
            checkpoint_path=data_dir / "checkpoints.db",
            max_queued_jobs=_env_int("DELIVERY_RECOVERY_MAX_QUEUED_JOBS", 4),
            max_pending_human=_env_int("DELIVERY_RECOVERY_MAX_PENDING_HUMAN", 100),
            lease_seconds=_env_int("DELIVERY_RECOVERY_LEASE_SECONDS", 30),
            pending_retention_seconds=_env_int(
                "DELIVERY_RECOVERY_PENDING_RETENTION_SECONDS", 86_400
            ),
        )

    def ensure(self) -> "StorageConfig":
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self
