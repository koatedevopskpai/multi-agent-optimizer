"""Application transaction store: runs, jobs, decisions, revisions and audit."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import StorageConfig
from .db import SCHEMA_VERSION, connect, migrate, utc_now

__all__ = ["ApplicationStore", "QueueFullError", "StoreError"]


class StoreError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class QueueFullError(StoreError):
    def __init__(self, message: str = "The runnable job queue is full.") -> None:
        super().__init__("QUEUE_FULL", message, status_code=429)


def _future(now: str, seconds: int) -> str:
    instant = datetime.fromisoformat(now)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return (instant + timedelta(seconds=seconds)).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _load(value: str | None) -> Any:
    return json.loads(value) if value else None


class ApplicationStore:
    def __init__(self, config: StorageConfig) -> None:
        self.config = config.ensure()
        self.schema_version = migrate(self.config.db_path)

    # ------------------------------------------------------------------ helpers
    def _connect(self) -> sqlite3.Connection:
        return connect(self.config.db_path)

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        record = dict(row)
        for key in (
            "request_json",
            "evaluation_json",
            "explanation_json",
            "provider_outcome_json",
            "pending_question_json",
            "pending_decision_json",
            "error_json",
            "resume_payload_json",
            "plan_json",
            "project_json",
            "payload_json",
        ):
            if key in record:
                record[key.removesuffix("_json")] = _load(record[key])
                del record[key]
        return record

    def _audit(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        event_type: str,
        *,
        actor_id: str | None = None,
        actor_role: str | None = None,
        decision_id: str | None = None,
        revision_id: str | None = None,
        payload: dict[str, Any] | None = None,
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events(
                event_id, run_id, event_type, actor_id, actor_role,
                decision_id, revision_id, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                run_id,
                event_type,
                actor_id,
                actor_role,
                decision_id,
                revision_id,
                _json(payload or {}),
                now,
            ),
        )

    def _queued_count(self, connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE state = 'queued'"
        ).fetchone()
        return int(row["count"])

    def _enforce_queue_capacity(self, connection: sqlite3.Connection) -> None:
        if self._queued_count(connection) >= self.config.max_queued_jobs:
            raise QueueFullError(
                f"The runnable job queue already holds {self.config.max_queued_jobs} jobs."
            )

    def queue_depth(self) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE state = 'queued'"
            ).fetchone()
            return int(row["count"])
        finally:
            connection.close()

    def pending_human_count(self) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM runs WHERE status IN ('needs_input', 'awaiting_approval')"
            ).fetchone()
            return int(row["count"])
        finally:
            connection.close()

    # --------------------------------------------------------------------- runs
    @staticmethod
    def scoped_idempotency_key(principal_id: str, key: str) -> str:
        return f"{principal_id}:{key}"

    def find_by_idempotency(
        self, principal_id: str, key: str
    ) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM runs WHERE idempotency_key = ?",
                (self.scoped_idempotency_key(principal_id, key),),
            ).fetchone()
            return self._row(row)
        finally:
            connection.close()

    def get_project_version(self, project_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM project_versions WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            return self._row(row)
        finally:
            connection.close()

    def create_run(
        self,
        *,
        draft_json: dict[str, Any],
        request_hash: str,
        run_id: str,
        thread_id: str,
        principal_id: str,
        idempotency_key: str | None,
        project_state: dict[str, Any] | None = None,
        now: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        now = now or utc_now()
        scoped_key = (
            self.scoped_idempotency_key(principal_id, idempotency_key)
            if idempotency_key
            else None
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if scoped_key:
                existing = connection.execute(
                    "SELECT * FROM runs WHERE idempotency_key = ?", (scoped_key,)
                ).fetchone()
                if existing is not None:
                    record = self._row(existing)
                    assert record is not None
                    if record["request_hash"] != request_hash:
                        raise StoreError(
                            "IDEMPOTENCY_CONFLICT",
                            "The idempotency key was already used with different content.",
                        )
                    connection.execute("COMMIT")
                    return record, False
            if project_state is not None:
                self._bind_project_state(connection, project_state, run_id, now)
            self._enforce_queue_capacity(connection)
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, thread_id, project_id, project_version, created_by,
                    status, request_json, request_hash, idempotency_key,
                    revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, 0, ?, ?)
                """,
                (
                    run_id,
                    thread_id,
                    str(draft_json.get("project_id", "DRAFT-PROJECT")),
                    int(draft_json.get("version", 1)),
                    principal_id,
                    _json(draft_json),
                    request_hash,
                    scoped_key,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO jobs(
                    run_id, state, available_at, attempts, created_at, updated_at
                ) VALUES (?, 'queued', ?, 0, ?, ?)
                """,
                (run_id, now, now, now),
            )
            self._audit(
                connection,
                run_id,
                "RUN_CREATED",
                actor_id=principal_id,
                actor_role="planner",
                payload={"status": "queued"},
                now=now,
            )
            connection.execute("COMMIT")
            record = self.get_run(run_id)
            assert record is not None
            return record, True
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _bind_project_state(
        self,
        connection: Any,
        project_state: dict[str, Any],
        run_id: str,
        now: str,
    ) -> None:
        """Bind run intake to the authoritative version/input/catalogue tuple.

        A catalogue, task, target or budget change is only an explicit versioned
        change: the same version must already carry the same input and catalogue
        hashes. A strictly newer version (current + 1) establishes the new
        authoritative tuple; skipped versions are rejected. Older versions are
        accepted only so that a genuinely stale run can be recorded and rejected
        by the decision transaction.
        """

        project_id = str(project_state["project_id"])
        incoming_version = int(project_state["version"])
        incoming_input = str(project_state["input_hash"])
        incoming_catalogue = str(project_state["catalogue_hash"])
        current = connection.execute(
            "SELECT * FROM project_versions WHERE project_id = ?", (project_id,)
        ).fetchone()
        if current is None:
            connection.execute(
                """
                INSERT INTO project_versions(
                    project_id, version, input_hash, catalogue_hash,
                    project_json, updated_at, updated_by_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    incoming_version,
                    incoming_input,
                    incoming_catalogue,
                    _json(project_state["project_json"]),
                    now,
                    run_id,
                ),
            )
            return

        current_version = int(current["version"])
        current_input = str(current["input_hash"])
        current_catalogue = str(current["catalogue_hash"])
        if incoming_version == current_version:
            if (
                incoming_input != current_input
                or incoming_catalogue != current_catalogue
            ):
                raise StoreError(
                    "PROJECT_VERSION_CONFLICT",
                    "The project version is already bound to different input or "
                    "catalogue content.",
                )
            return
        if incoming_version == current_version + 1:
            updated = connection.execute(
                """
                UPDATE project_versions SET
                    version = ?, input_hash = ?, catalogue_hash = ?,
                    project_json = ?, updated_at = ?, updated_by_run_id = ?,
                    updated_by_decision_id = NULL
                WHERE project_id = ? AND version = ? AND input_hash = ?
                    AND catalogue_hash = ?
                """,
                (
                    incoming_version,
                    incoming_input,
                    incoming_catalogue,
                    _json(project_state["project_json"]),
                    now,
                    run_id,
                    project_id,
                    current_version,
                    current_input,
                    current_catalogue,
                ),
            )
            if updated.rowcount != 1:
                raise StoreError(
                    "PROJECT_VERSION_CONFLICT",
                    "The current project version changed while binding the run.",
                )
            return
        if incoming_version < current_version:
            return
        raise StoreError(
            "PROJECT_VERSION_CONFLICT",
            "Project versions must advance one at a time; a skipped version is "
            "not an explicit catalogue update.",
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return self._row(row)
        finally:
            connection.close()

    def get_job(self, run_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM jobs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return self._row(row)
        finally:
            connection.close()

    # --------------------------------------------------------------------- jobs
    def expire_pending(self, now: str | None = None) -> int:
        now = now or utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT run_id FROM runs
                WHERE status IN ('needs_input', 'awaiting_approval')
                  AND retention_expires_at IS NOT NULL
                  AND retention_expires_at < ?
                """,
                (now,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE runs SET status = 'expired', updated_at = ? WHERE run_id = ?",
                    (now, row["run_id"]),
                )
                connection.execute(
                    "UPDATE jobs SET state = 'expired', lease_owner = NULL, "
                    "lease_expires_at = NULL, updated_at = ? WHERE run_id = ?",
                    (now, row["run_id"]),
                )
                self._audit(
                    connection,
                    row["run_id"],
                    "RUN_EXPIRED",
                    payload={"reason": "pending_retention_expired"},
                    now=now,
                )
            connection.execute("COMMIT")
            return len(rows)
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def recover_expired_leases(self, now: str | None = None) -> int:
        now = now or utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT run_id FROM jobs
                WHERE state = 'running' AND lease_expires_at IS NOT NULL
                  AND lease_expires_at < ?
                """,
                (now,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE jobs SET state = 'queued', lease_owner = NULL,
                        lease_expires_at = NULL, available_at = ?, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (now, now, row["run_id"]),
                )
                connection.execute(
                    "UPDATE runs SET status = 'queued', updated_at = ? WHERE run_id = ?",
                    (now, row["run_id"]),
                )
                self._audit(
                    connection,
                    row["run_id"],
                    "LEASE_EXPIRED_RECLAIMED",
                    payload={"recovered_at": now},
                    now=now,
                )
            connection.execute("COMMIT")
            return len(rows)
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def claim_next(
        self,
        worker_id: str,
        *,
        now: str | None = None,
    ) -> dict[str, Any] | None:
        now = now or utc_now()
        self.expire_pending(now)
        self.recover_expired_leases(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """
                SELECT 1 FROM jobs
                WHERE state = 'running'
                  AND (lease_expires_at IS NULL OR lease_expires_at >= ?)
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if active is not None:
                connection.execute("COMMIT")
                return None
            row = connection.execute(
                """
                SELECT jobs.run_id FROM jobs
                JOIN runs ON runs.run_id = jobs.run_id
                WHERE jobs.state = 'queued' AND jobs.available_at <= ?
                  AND runs.status IN ('queued', 'approved', 'rejected')
                ORDER BY jobs.available_at, jobs.created_at
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            run_id = row["run_id"]
            connection.execute(
                """
                UPDATE jobs SET state = 'running', lease_owner = ?,
                    lease_expires_at = ?, attempts = attempts + 1, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    worker_id,
                    _future(now, self.config.lease_seconds),
                    now,
                    run_id,
                ),
            )
            connection.execute(
                "UPDATE runs SET status = 'running', updated_at = ? WHERE run_id = ?",
                (now, run_id),
            )
            connection.execute("COMMIT")
            run = self.get_run(run_id)
            job = self.get_job(run_id)
            assert run is not None and job is not None
            return {"run": run, "job": job}
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def heartbeat(self, run_id: str, worker_id: str, *, now: str | None = None) -> bool:
        now = now or utc_now()
        connection = self._connect()
        try:
            cursor = connection.execute(
                """
                UPDATE jobs SET lease_expires_at = ?, updated_at = ?
                WHERE run_id = ? AND lease_owner = ? AND state = 'running'
                """,
                (_future(now, self.config.lease_seconds), now, run_id, worker_id),
            )
            return cursor.rowcount == 1
        finally:
            connection.close()

    def mark_waiting(
        self,
        run_id: str,
        *,
        status: str,
        pending: dict[str, Any] | None,
        now: str | None = None,
    ) -> None:
        if status not in {"needs_input", "awaiting_approval"}:
            raise ValueError(f"unsupported waiting status: {status}")
        now = now or utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if status == "needs_input":
                connection.execute(
                    """
                    UPDATE runs SET status = ?, pending_question_json = ?,
                        pending_decision_json = NULL, waiting_since = ?,
                        retention_expires_at = ?, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        status,
                        _json(pending) if pending is not None else None,
                        now,
                        _future(now, self.config.pending_retention_seconds),
                        now,
                        run_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE runs SET status = ?, pending_decision_json = ?,
                        pending_question_json = NULL, waiting_since = ?,
                        retention_expires_at = ?, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        status,
                        _json(pending) if pending is not None else None,
                        now,
                        _future(now, self.config.pending_retention_seconds),
                        now,
                        run_id,
                    ),
                )
            connection.execute(
                """
                UPDATE jobs SET state = ?, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE run_id = ?
                """,
                (status, now, run_id),
            )
            pending_count = connection.execute(
                "SELECT COUNT(*) AS count FROM runs WHERE status IN ('needs_input', 'awaiting_approval')"
            ).fetchone()["count"]
            if int(pending_count) > self.config.max_pending_human:
                connection.execute(
                    "UPDATE runs SET status = 'expired', updated_at = ? WHERE run_id = ?",
                    (now, run_id),
                )
                connection.execute(
                    "UPDATE jobs SET state = 'expired', updated_at = ? WHERE run_id = ?",
                    (now, run_id),
                )
                raise StoreError(
                    "PENDING_RETENTION_LIMIT",
                    "Pending human work exceeded the configured retention bound.",
                    status_code=429,
                )
            self._audit(
                connection,
                run_id,
                "RUN_WAITING",
                payload={"status": status},
                now=now,
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def enqueue_resume(
        self,
        run_id: str,
        *,
        kind: str,
        payload: dict[str, Any],
        now: str | None = None,
    ) -> None:
        now = now or utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._enforce_queue_capacity(connection)
            connection.execute(
                """
                UPDATE jobs SET state = 'queued', resume_kind = ?,
                    resume_payload_json = ?, available_at = ?, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE run_id = ?
                """,
                (kind, _json(payload), now, now, run_id),
            )
            connection.execute(
                """
                UPDATE runs SET status = 'queued', waiting_since = NULL,
                    retention_expires_at = NULL, updated_at = ?
                WHERE run_id = ?
                """,
                (now, run_id),
            )
            self._audit(
                connection,
                run_id,
                "RESUME_QUEUED",
                payload={"kind": kind},
                now=now,
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def update_result(
        self,
        run_id: str,
        *,
        evaluation: dict[str, Any] | None = None,
        explanation: dict[str, Any] | None = None,
        provider_outcome: dict[str, Any] | None = None,
        now: str | None = None,
    ) -> None:
        now = now or utc_now()
        connection = self._connect()
        try:
            connection.execute(
                """
                UPDATE runs SET
                    evaluation_json = COALESCE(?, evaluation_json),
                    explanation_json = COALESCE(?, explanation_json),
                    provider_outcome_json = COALESCE(?, provider_outcome_json),
                    updated_at = ?
                WHERE run_id = ?
                """,
                (
                    _json(evaluation) if evaluation is not None else None,
                    _json(explanation) if explanation is not None else None,
                    _json(provider_outcome) if provider_outcome is not None else None,
                    now,
                    run_id,
                ),
            )
        finally:
            connection.close()

    def complete_run(
        self,
        run_id: str,
        *,
        status: str,
        evaluation: dict[str, Any] | None = None,
        explanation: dict[str, Any] | None = None,
        provider_outcome: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        now: str | None = None,
    ) -> None:
        now = now or utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE runs SET status = ?, evaluation_json = COALESCE(?, evaluation_json),
                    explanation_json = COALESCE(?, explanation_json),
                    provider_outcome_json = COALESCE(?, provider_outcome_json),
                    error_json = ?, pending_question_json = NULL,
                    pending_decision_json = NULL, waiting_since = NULL,
                    retention_expires_at = NULL, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    status,
                    _json(evaluation) if evaluation is not None else None,
                    _json(explanation) if explanation is not None else None,
                    _json(provider_outcome) if provider_outcome is not None else None,
                    _json(error) if error is not None else None,
                    now,
                    run_id,
                ),
            )
            connection.execute(
                """
                UPDATE jobs SET state = ?, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE run_id = ?
                """,
                (status, now, run_id),
            )
            self._audit(
                connection,
                run_id,
                "RUN_COMPLETED",
                payload={"status": status},
                now=now,
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def fail_run(
        self,
        run_id: str,
        *,
        code: str,
        message: str,
        now: str | None = None,
    ) -> None:
        self.complete_run(
            run_id,
            status="failed",
            error={"code": code, "message": message},
            now=now,
        )

    # ----------------------------------------------------------------- decisions
    def get_decision(self, run_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM decisions WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            return self._row(row)
        finally:
            connection.close()

    def get_decision_by_key(self, run_id: str, key: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM decisions WHERE run_id = ? AND idempotency_key = ?",
                (run_id, key),
            ).fetchone()
            return self._row(row)
        finally:
            connection.close()

    def commit_decision(
        self,
        run_id: str,
        *,
        decision_id: str,
        revision_id: str | None,
        candidate_id: str,
        project_version: int,
        input_hash: str,
        catalogue_hash: str,
        approved: bool,
        actor_id: str,
        actor_role: str,
        idempotency_key: str,
        request_hash: str,
        revised_plan: dict[str, Any] | None,
        current_project_state: dict[str, Any] | None = None,
        next_project_state: dict[str, Any] | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        now = now or utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise StoreError("RUN_NOT_FOUND", "Run not found.", status_code=404)
            existing = connection.execute(
                "SELECT * FROM decisions WHERE run_id = ? AND idempotency_key = ?",
                (run_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                record = self._row(existing)
                assert record is not None
                if record["request_hash"] != request_hash:
                    raise StoreError(
                        "IDEMPOTENCY_CONFLICT",
                        "The idempotency key was already used with different decision content.",
                    )
                connection.execute("COMMIT")
                return record
            if run["status"] != "awaiting_approval":
                raise StoreError(
                    "DECISION_CONFLICT",
                    f"Run is {run['status']!r}, not awaiting approval.",
                )
            project_row = connection.execute(
                "SELECT * FROM project_versions WHERE project_id = ?",
                (run["project_id"],),
            ).fetchone()
            if project_row is None and current_project_state is not None:
                connection.execute(
                    """
                    INSERT INTO project_versions(
                        project_id, version, input_hash, catalogue_hash,
                        project_json, updated_at, updated_by_run_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        current_project_state["project_id"],
                        int(current_project_state["version"]),
                        str(current_project_state["input_hash"]),
                        str(current_project_state["catalogue_hash"]),
                        _json(current_project_state["project_json"]),
                        now,
                        run_id,
                    ),
                )
                project_row = connection.execute(
                    "SELECT * FROM project_versions WHERE project_id = ?",
                    (run["project_id"],),
                ).fetchone()
            if project_row is None:
                raise StoreError(
                    "STALE_DECISION",
                    "The current project version is not established.",
                )
            if (
                int(project_row["version"]) != project_version
                or str(project_row["input_hash"]) != input_hash
                or str(project_row["catalogue_hash"]) != catalogue_hash
            ):
                raise StoreError(
                    "STALE_DECISION",
                    "The current project version, input or catalogue has advanced.",
                )
            self._enforce_queue_capacity(connection)
            connection.execute(
                """
                INSERT INTO decisions(
                    decision_id, run_id, candidate_id, project_version, input_hash,
                    catalogue_hash, approved, actor_id, actor_role, idempotency_key,
                    request_hash, revision_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    run_id,
                    candidate_id,
                    project_version,
                    input_hash,
                    catalogue_hash,
                    1 if approved else 0,
                    actor_id,
                    actor_role,
                    idempotency_key,
                    request_hash,
                    revision_id,
                    now,
                ),
            )
            revision_number = int(run["revision"])
            if approved:
                if revision_id is None or revised_plan is None:
                    raise StoreError(
                        "DECISION_INVALID", "An approved decision needs a revision."
                    )
                revision_number += 1
                connection.execute(
                    """
                    INSERT INTO revisions(
                        revision_id, run_id, revision, project_version, candidate_id,
                        plan_json, input_hash, catalogue_hash, decision_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        run_id,
                        revision_number,
                        project_version + 1,
                        candidate_id,
                        _json(revised_plan),
                        input_hash,
                        catalogue_hash,
                        decision_id,
                        now,
                    ),
                )
                if next_project_state is None:
                    raise StoreError(
                        "DECISION_INVALID",
                        "An approved decision needs the next project state.",
                    )
                advanced = connection.execute(
                    """
                    UPDATE project_versions SET
                        version = ?, input_hash = ?, catalogue_hash = ?,
                        project_json = ?, updated_at = ?, updated_by_run_id = ?,
                        updated_by_decision_id = ?
                    WHERE project_id = ? AND version = ? AND input_hash = ?
                        AND catalogue_hash = ?
                    """,
                    (
                        int(next_project_state["version"]),
                        str(next_project_state["input_hash"]),
                        str(next_project_state["catalogue_hash"]),
                        _json(next_project_state["project_json"]),
                        now,
                        run_id,
                        decision_id,
                        run["project_id"],
                        project_version,
                        input_hash,
                        catalogue_hash,
                    ),
                )
                if advanced.rowcount != 1:
                    raise StoreError(
                        "STALE_DECISION",
                        "The current project version advanced during the decision.",
                    )
            status = "approved" if approved else "rejected"
            connection.execute(
                """
                UPDATE runs SET status = ?, decision_status = ?, revision = ?,
                    updated_at = ? WHERE run_id = ?
                """,
                (status, status, revision_number, now, run_id),
            )
            connection.execute(
                """
                UPDATE jobs SET state = 'queued', resume_kind = 'decision',
                    resume_payload_json = ?, available_at = ?, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE run_id = ?
                """,
                (_json({"decision_id": decision_id}), now, now, run_id),
            )
            self._audit(
                connection,
                run_id,
                "DECISION_APPROVED" if approved else "DECISION_REJECTED",
                actor_id=actor_id,
                actor_role=actor_role,
                decision_id=decision_id,
                revision_id=revision_id,
                payload={
                    "candidate_id": candidate_id,
                    "project_version": project_version,
                    "input_hash": input_hash,
                    "catalogue_hash": catalogue_hash,
                },
                now=now,
            )
            connection.execute("COMMIT")
            record = connection.execute(
                "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)
            ).fetchone()
            result = self._row(record)
            assert result is not None
            return result
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def add_audit(
        self,
        run_id: str,
        event_type: str,
        *,
        actor_id: str | None = None,
        actor_role: str | None = None,
        payload: dict[str, Any] | None = None,
        now: str | None = None,
    ) -> None:
        now = now or utc_now()
        connection = self._connect()
        try:
            self._audit(
                connection,
                run_id,
                event_type,
                actor_id=actor_id,
                actor_role=actor_role,
                payload=payload,
                now=now,
            )
        finally:
            connection.close()

    def list_audit(self, run_id: str) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM audit_events WHERE run_id = ? ORDER BY created_at, event_id",
                (run_id,),
            ).fetchall()
            return [self._row(row) for row in rows if row is not None]
        finally:
            connection.close()

    def list_revisions(self, run_id: str) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM revisions WHERE run_id = ? ORDER BY revision",
                (run_id,),
            ).fetchall()
            return [self._row(row) for row in rows if row is not None]
        finally:
            connection.close()

    # ------------------------------------------------------------------ health
    def ready(self) -> bool:
        try:
            connection = self._connect()
            try:
                version = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
                ).fetchone()["version"]
                if int(version) != SCHEMA_VERSION:
                    return False
                connection.execute("SELECT 1 FROM runs LIMIT 1").fetchone()
                return True
            finally:
                connection.close()
        except Exception:
            return False

    def worker_heartbeat(self, worker_id: str, *, now: str | None = None) -> None:
        now = now or utc_now()
        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT INTO worker_heartbeats(worker_id, started_at, heartbeat_at)
                VALUES (?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET heartbeat_at = excluded.heartbeat_at
                """,
                (worker_id, now, now),
            )
        finally:
            connection.close()

    def worker_ready(self, worker_id: str, *, max_age_seconds: int = 15) -> bool:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT heartbeat_at FROM worker_heartbeats WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
            if row is None:
                return False
            stamp = datetime.fromisoformat(row["heartbeat_at"])
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            return (now - stamp).total_seconds() <= max_age_seconds
        finally:
            connection.close()
