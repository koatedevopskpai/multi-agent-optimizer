"""SQLite migrations for the application transaction store."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["SCHEMA_VERSION", "connect", "migrate", "utc_now"]

SCHEMA_VERSION = 2

_MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    project_version INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    status TEXT NOT NULL,
    request_json TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    evaluation_json TEXT,
    explanation_json TEXT,
    provider_outcome_json TEXT,
    pending_question_json TEXT,
    pending_decision_json TEXT,
    decision_status TEXT,
    revision INTEGER NOT NULL DEFAULT 0,
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    waiting_since TEXT,
    retention_expires_at TEXT
);

CREATE INDEX IF NOT EXISTS runs_status_idx ON runs(status);

CREATE TABLE IF NOT EXISTS jobs (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
    state TEXT NOT NULL,
    resume_kind TEXT,
    resume_payload_json TEXT,
    available_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS jobs_state_idx ON jobs(state, available_at);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    candidate_id TEXT NOT NULL,
    project_version INTEGER NOT NULL,
    input_hash TEXT NOT NULL,
    catalogue_hash TEXT NOT NULL,
    approved INTEGER NOT NULL,
    actor_id TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    revision_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS revisions (
    revision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    revision INTEGER NOT NULL,
    project_version INTEGER NOT NULL,
    candidate_id TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    catalogue_hash TEXT NOT NULL,
    decision_id TEXT NOT NULL REFERENCES decisions(decision_id),
    created_at TEXT NOT NULL,
    UNIQUE(run_id, revision)
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    event_type TEXT NOT NULL,
    actor_id TEXT,
    actor_role TEXT,
    decision_id TEXT,
    revision_id TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS audit_run_idx ON audit_events(run_id, created_at);

CREATE TABLE IF NOT EXISTS worker_heartbeats (
    worker_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""

_MIGRATION_2 = """
CREATE TABLE IF NOT EXISTS project_versions (
    project_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    input_hash TEXT NOT NULL,
    catalogue_hash TEXT NOT NULL,
    project_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by_run_id TEXT,
    updated_by_decision_id TEXT
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def migrate(path: Path | str) -> int:
    connection = connect(path)
    try:
        connection.executescript(_MIGRATION_1)
        connection.executescript(_MIGRATION_2)
        current = connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
        ).fetchone()["version"]
        if current < 1:
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)",
                (utc_now(),),
            )
            current = 1
        if current < 2:
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (2, ?)",
                (utc_now(),),
            )
            current = 2
        return int(current)
    finally:
        connection.close()
