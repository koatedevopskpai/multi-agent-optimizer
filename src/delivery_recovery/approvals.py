"""Authorised, atomic and idempotent recovery decisions."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict

from .candidates import evaluate_recovery
from .contracts import ProjectInput
from .storage.store import ApplicationStore, StoreError
from .workflow.contracts import DraftRecoveryRequest

__all__ = [
    "DecisionRequest",
    "DecisionService",
    "authorise_run_access",
    "authorise_run_create",
]


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    project_version: int
    input_hash: str
    catalogue_hash: str
    approved: bool


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def authorise_run_access(run: dict[str, Any], principal: Any) -> None:
    if not principal.can_access(str(run["project_id"])):
        raise StoreError(
            "FORBIDDEN",
            "The principal cannot access this run's project.",
            status_code=403,
        )


def authorise_run_create(principal: Any, draft: DraftRecoveryRequest) -> None:
    """Authorise project creation before any persistence or idempotency lookup.

    Incomplete drafts without a concrete project ID are allowed only for a
    principal with wildcard project scope. A concrete project must be named in
    the principal's permitted projects.
    """

    if not (principal.is_planner() or principal.is_manager()):
        raise StoreError(
            "FORBIDDEN",
            "The principal cannot create runs.",
            status_code=403,
        )
    project_id = draft.project_id or "DRAFT-PROJECT"
    if project_id == "DRAFT-PROJECT":
        if "*" not in principal.project_ids:
            raise StoreError(
                "PROJECT_FORBIDDEN",
                "A project-less draft requires wildcard project scope.",
                status_code=403,
            )
        return
    if not principal.can_access(project_id):
        raise StoreError(
            "PROJECT_FORBIDDEN",
            "The principal cannot create a run for this project.",
            status_code=403,
        )


class DecisionService:
    def __init__(self, store: ApplicationStore) -> None:
        self.store = store

    def submit(
        self,
        run_id: str,
        request: DecisionRequest,
        *,
        principal: Any,
        idempotency_key: str,
        now: str | None = None,
    ) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc).isoformat()
        run = self.store.get_run(run_id)
        if run is None:
            raise StoreError("RUN_NOT_FOUND", "Run not found.", status_code=404)
        authorise_run_access(run, principal)
        if not principal.is_manager():
            raise StoreError(
                "FORBIDDEN",
                "Only an authorised manager can decide.",
                status_code=403,
            )

        request_payload = request.model_dump(mode="json")
        request_hash = _hash(request_payload)
        existing = self.store.get_decision_by_key(run_id, idempotency_key)
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise StoreError(
                    "IDEMPOTENCY_CONFLICT",
                    "The idempotency key was already used with different decision content.",
                )
            return {
                "decision": existing,
                "revision": self._revision_for_decision(run_id, existing),
                "idempotent_replay": True,
            }

        if run["status"] != "awaiting_approval":
            raise StoreError(
                "DECISION_CONFLICT",
                f"Run is {run['status']!r}, not awaiting approval.",
            )

        request_json = run["request"]
        draft = DraftRecoveryRequest.model_validate(request_json)
        project = draft.to_project_input()
        disruption = draft.disruption
        evaluation = evaluate_recovery(project, disruption)
        candidate = next(
            (
                item
                for item in evaluation.candidates
                if item.candidate_id == request.candidate_id
            ),
            None,
        )
        if candidate is None:
            raise StoreError(
                "STALE_DECISION",
                "The candidate is not present in the current evaluation.",
            )
        if request.approved and not candidate.feasible:
            raise StoreError(
                "CANDIDATE_NOT_FEASIBLE",
                "The candidate is not feasible in the current evaluation.",
                status_code=422,
            )
        expected = {
            "project_version": project.version,
            "input_hash": candidate.base_hash,
            "catalogue_hash": candidate.catalogue_hash,
        }
        supplied = {
            "project_version": request.project_version,
            "input_hash": request.input_hash,
            "catalogue_hash": request.catalogue_hash,
        }
        if supplied != expected:
            raise StoreError(
                "STALE_DECISION",
                "The decision does not match the current project, input or catalogue.",
            )

        pending = run.get("pending_decision") or {}
        if pending:
            for field in ("candidate_id", "project_version", "input_hash", "catalogue_hash"):
                if pending.get(field) != getattr(request, field):
                    raise StoreError(
                        "STALE_DECISION",
                        f"The pending decision field {field!r} changed.",
                    )

        decision_id = uuid.uuid4().hex
        revision_id = uuid.uuid4().hex if request.approved else None
        current_project_state = {
            "project_id": project.project_id,
            "version": project.version,
            "input_hash": candidate.base_hash,
            "catalogue_hash": candidate.catalogue_hash,
            "project_json": project.model_dump(mode="json"),
        }
        revised_plan = None
        next_project_state = None
        if request.approved:
            revised_plan = self._revised_plan(
                project, disruption, candidate.option_ids
            )
            revised_project = ProjectInput.model_validate(revised_plan)
            revised_evaluation = evaluate_recovery(
                revised_project, None, option_ids=[]
            )
            if (
                revised_evaluation.baseline.finish_offset_workdays
                != candidate.finish_offset_workdays
            ):
                raise StoreError(
                    "REVISION_MISMATCH",
                    "The persisted revision does not reproduce the approved finish.",
                )
            next_candidate = revised_evaluation.candidates[0]
            next_project_state = {
                "project_id": revised_project.project_id,
                "version": revised_project.version,
                "input_hash": next_candidate.base_hash,
                "catalogue_hash": next_candidate.catalogue_hash,
                "project_json": revised_project.model_dump(mode="json"),
            }
        decision = self.store.commit_decision(
            run_id,
            decision_id=decision_id,
            revision_id=revision_id,
            candidate_id=candidate.candidate_id,
            project_version=project.version,
            input_hash=candidate.base_hash,
            catalogue_hash=candidate.catalogue_hash,
            approved=request.approved,
            actor_id=principal.actor_id,
            actor_role="manager",
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            revised_plan=revised_plan,
            current_project_state=current_project_state,
            next_project_state=next_project_state,
            now=now,
        )
        return {
            "decision": decision,
            "revision": self._revision_for_decision(run_id, decision),
            "idempotent_replay": False,
        }

    def _revision_for_decision(
        self, run_id: str, decision: dict[str, Any]
    ) -> dict[str, Any] | None:
        revision_id = decision.get("revision_id")
        if not revision_id:
            return None
        for revision in self.store.list_revisions(run_id):
            if revision["revision_id"] == revision_id:
                return revision
        return None

    @staticmethod
    def _revised_plan(
        project: ProjectInput,
        disruption: Any,
        option_ids: list[str],
    ) -> dict[str, Any]:
        durations = {task.id: task.duration_workdays for task in project.tasks}
        if disruption is not None:
            durations[disruption.task_id] = disruption.revised_duration_workdays
        by_option = {option.option_id: option for option in project.catalogue}
        for option_id in option_ids:
            option = by_option[option_id]
            for change in option.changes:
                durations[change.task_id] = change.new_duration_workdays
        payload = project.model_dump(mode="json")
        payload["version"] = project.version + 1
        payload["tasks"] = [
            {
                **task,
                "duration_workdays": durations[task["id"]],
            }
            for task in payload["tasks"]
        ]
        return payload
