"""FastAPI application: deterministic evaluation plus durable Phase 3 runs."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from multi_agent_optimizer.cpm import ProjectError
from pydantic import ValidationError

from ..approvals import (
    DecisionRequest,
    authorise_run_access,
    authorise_run_create,
)
from ..candidates import evaluate_recovery
from ..contracts import PolicyError, RecoveryEvaluation
from ..storage.store import QueueFullError, StoreError
from ..workflow.contracts import DraftRecoveryRequest
from .contracts import ClarificationSubmit, RecoveryRequest, RunCreateRequest
from .identity import IdentityProvider, Principal, get_principal
from .middleware import JSONBodyGuardMiddleware
from .runtime import Phase3Runtime

__all__ = ["app", "create_app"]

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _error(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


def create_app(
    runtime: Phase3Runtime | None = None,
    *,
    web_dist: Path | None = None,
) -> FastAPI:
    application = FastAPI(
        title="Delivery Recovery Assistant",
        version="0.3.0",
        description=(
            "Deterministic recovery comparison plus durable local runs, "
            "authorised decisions and audit. CPM facts are authoritative."
        ),
    )
    application.state.runtime = runtime
    application.state.identity = runtime.identity if runtime else IdentityProvider()
    application.add_middleware(JSONBodyGuardMiddleware)

    @application.exception_handler(PolicyError)
    async def _policy_error_handler(_: Request, exc: PolicyError) -> JSONResponse:
        return _error(exc.code, str(exc), 422)

    @application.exception_handler(ProjectError)
    async def _project_error_handler(_: Request, exc: ProjectError) -> JSONResponse:
        return _error("INVALID_PROJECT", str(exc), 422)

    @application.exception_handler(StoreError)
    async def _store_error_handler(_: Request, exc: StoreError) -> JSONResponse:
        return _error(exc.code, exc.message, exc.status_code)

    def require_runtime(request: Request) -> Phase3Runtime:
        configured: Phase3Runtime | None = request.app.state.runtime
        if configured is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "PHASE3_NOT_CONFIGURED",
                    "message": "Durable run runtime is not configured.",
                },
            )
        return configured

    @application.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/readyz")
    def readyz(request: Request):
        configured: Phase3Runtime | None = request.app.state.runtime
        if configured is None:
            return {"status": "ready", "mode": "deterministic"}
        payload = configured.ready()
        if payload["status"] != "ready":
            return JSONResponse(status_code=503, content=payload)
        return payload

    @application.post("/v1/recovery/evaluations", response_model=RecoveryEvaluation)
    def evaluate(request: RecoveryRequest) -> RecoveryEvaluation:
        return evaluate_recovery(
            request.project,
            request.disruption,
            option_ids=request.option_ids,
        )

    @application.post("/v1/runs", status_code=202)
    def create_run(
        body: RunCreateRequest,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        principal: Principal = Depends(get_principal),
    ):
        configured = require_runtime(request)
        draft = DraftRecoveryRequest.model_validate(body.draft)
        authorise_run_create(principal, draft)
        request_hash = _hash(draft.model_dump(mode="json"))
        project_state = None
        try:
            project = draft.to_project_input()
            evaluation = evaluate_recovery(project, draft.disruption)
            base_candidate = evaluation.candidates[0]
            project_state = {
                "project_id": project.project_id,
                "version": project.version,
                "input_hash": base_candidate.base_hash,
                "catalogue_hash": base_candidate.catalogue_hash,
                "project_json": project.model_dump(mode="json"),
            }
        except (ValueError, ValidationError):
            # Incomplete drafts are legitimately project-scoped only after
            # clarification; the decision transaction seeds the state later.
            project_state = None
        run_id = uuid.uuid4().hex
        record, created = configured.store.create_run(
            draft_json=draft.model_dump(mode="json"),
            request_hash=request_hash,
            run_id=run_id,
            thread_id=run_id,
            principal_id=principal.actor_id,
            idempotency_key=idempotency_key,
            project_state=project_state,
        )
        return {
            "run_id": record["run_id"],
            "thread_id": record["thread_id"],
            "status": record["status"],
            "status_url": f"/v1/runs/{record['run_id']}",
            "idempotent_replay": not created,
        }

    def _run_view(record: dict[str, Any], principal: Principal) -> dict[str, Any]:
        return {
            "run_id": record["run_id"],
            "thread_id": record["thread_id"],
            "status": record["status"],
            "project_id": record["project_id"],
            "project_version": record["project_version"],
            "created_by": record["created_by"],
            "revision": record["revision"],
            "decision_status": record.get("decision_status"),
            "evaluation": record.get("evaluation"),
            "explanation": record.get("explanation"),
            "provider_outcome": record.get("provider_outcome"),
            "pending_question": record.get("pending_question"),
            "pending_decision": record.get("pending_decision"),
            "error": record.get("error"),
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "retention_expires_at": record.get("retention_expires_at"),
            "access": {
                "actor_id": principal.actor_id,
                "roles": list(principal.roles),
            },
        }

    @application.get("/v1/runs/{run_id}")
    def get_run(
        run_id: str,
        request: Request,
        principal: Principal = Depends(get_principal),
    ):
        configured = require_runtime(request)
        record = configured.store.get_run(run_id)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "RUN_NOT_FOUND", "message": "Run not found."},
            )
        authorise_run_access(record, principal)
        return _run_view(record, principal)

    @application.post("/v1/runs/{run_id}/clarifications", status_code=202)
    def clarify(
        run_id: str,
        body: ClarificationSubmit,
        request: Request,
        principal: Principal = Depends(get_principal),
    ):
        configured = require_runtime(request)
        record = configured.store.get_run(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND"})
        authorise_run_access(record, principal)
        if record["status"] != "needs_input":
            raise StoreError(
                "CLARIFICATION_CONFLICT",
                f"Run is {record['status']!r}, not awaiting clarification.",
            )
        pending = record.get("pending_question") or {}
        if pending.get("field") != body.field:
            raise StoreError(
                "STALE_CLARIFICATION",
                "The clarification does not match the pending question.",
            )
        configured.store.enqueue_resume(
            run_id,
            kind="clarification",
            payload={"field": body.field, "answer": body.answer},
        )
        configured.store.add_audit(
            run_id,
            "CLARIFICATION_RECEIVED",
            actor_id=principal.actor_id,
            actor_role="planner" if principal.is_planner() else "manager",
            payload={"field": body.field},
        )
        return {"run_id": run_id, "status": "queued"}

    @application.post("/v1/runs/{run_id}/decisions")
    def decide(
        run_id: str,
        body: DecisionRequest,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        principal: Principal = Depends(get_principal),
    ):
        if not idempotency_key:
            raise StoreError(
                "IDEMPOTENCY_KEY_REQUIRED",
                "An Idempotency-Key header is required for decisions.",
                status_code=400,
            )
        configured = require_runtime(request)
        result = configured.decisions.submit(
            run_id,
            body,
            principal=principal,
            idempotency_key=idempotency_key,
        )
        return result

    @application.get("/v1/runs/{run_id}/audit")
    def audit(
        run_id: str,
        request: Request,
        principal: Principal = Depends(get_principal),
    ):
        configured = require_runtime(request)
        record = configured.store.get_run(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND"})
        authorise_run_access(record, principal)
        return {
            "run_id": run_id,
            "events": configured.store.list_audit(run_id),
            "revisions": configured.store.list_revisions(run_id),
        }

    dist = web_dist or (_REPO_ROOT / "web" / "dist")
    if dist.is_dir():
        application.mount("/ui", StaticFiles(directory=dist, html=True), name="ui")

        @application.get("/", include_in_schema=False)
        def index() -> RedirectResponse:
            return RedirectResponse(url="/ui/")
    else:

        @application.get("/", include_in_schema=False)
        def index_placeholder() -> HTMLResponse:
            return HTMLResponse(
                "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
                "<title>Delivery Recovery Assistant</title></head><body>"
                "<h1>Delivery Recovery Assistant</h1>"
                "<p>The API is running. Build the UI with "
                "<code>cd web &amp;&amp; npm install &amp;&amp; npm run build</code>, "
                "or use <a href='/docs'>/docs</a>.</p>"
                "</body></html>"
            )

    return application


app = create_app()
