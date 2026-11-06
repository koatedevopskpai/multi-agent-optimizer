"""Run the Phase 3 local reference application."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import uvicorn  # noqa: E402

from delivery_recovery.api.app import create_app  # noqa: E402
from delivery_recovery.api.runtime import build_runtime  # noqa: E402
from delivery_recovery.storage.config import StorageConfig  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--data-dir",
        default=str(REPO / "var" / "delivery-recovery"),
    )
    args = parser.parse_args()
    data_dir = Path(args.data_dir).resolve()
    config = StorageConfig.from_env()
    if args.data_dir:
        config = StorageConfig(
            data_dir=data_dir,
            db_path=data_dir / "application.db",
            checkpoint_path=data_dir / "checkpoints.db",
            max_queued_jobs=config.max_queued_jobs,
            max_pending_human=config.max_pending_human,
            lease_seconds=config.lease_seconds,
            pending_retention_seconds=config.pending_retention_seconds,
        )
    runtime = build_runtime(config, start_worker=True)
    application = create_app(runtime)
    print(f"UI: http://{args.host}:{args.port}/ui/")
    print(f"OpenAPI: http://{args.host}:{args.port}/docs")
    print(f"Identity file: {data_dir / 'demo-identities.json'}")
    try:
        uvicorn.run(application, host=args.host, port=args.port, log_level="info")
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
