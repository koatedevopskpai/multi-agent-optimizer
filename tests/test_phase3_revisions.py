"""Phase 3 R3: persisted revisions must preserve disrupted state."""

from __future__ import annotations

from delivery_recovery.approvals import DecisionRequest
from delivery_recovery.candidates import evaluate_recovery
from delivery_recovery.contracts import ProjectInput

from phase3_support import build_test_runtime, draft


def _manager(runtime):
    return next(
        identity.principal
        for identity in runtime.identity.identities
        if identity.principal.is_manager()
    )


def _approve(runtime, value, run_id: str, key: str) -> dict:
    project = value.to_project_input()
    evaluation = evaluate_recovery(project, value.disruption)
    base = evaluation.candidates[0]
    runtime.store.create_run(
        draft_json=value.model_dump(mode="json"),
        request_hash=f"hash-{run_id}",
        run_id=run_id,
        thread_id=run_id,
        principal_id="planner.demo",
        idempotency_key=f"create-{run_id}",
        project_state={
            "project_id": project.project_id,
            "version": project.version,
            "input_hash": base.base_hash,
            "catalogue_hash": base.catalogue_hash,
            "project_json": project.model_dump(mode="json"),
        },
    )
    assert runtime.worker.run_once()["status"] == "awaiting_approval"
    run = runtime.store.get_run(run_id)
    binding = run["pending_decision"]
    result = runtime.decisions.submit(
        run_id,
        DecisionRequest(
            candidate_id=binding["candidate_id"],
            project_version=binding["project_version"],
            input_hash=binding["input_hash"],
            catalogue_hash=binding["catalogue_hash"],
            approved=True,
        ),
        principal=_manager(runtime),
        idempotency_key=key,
    )
    return result["revision"]


def _durations(plan: dict) -> dict[str, int]:
    return {task["id"]: task["duration_workdays"] for task in plan["tasks"]}


def test_install_support_preserves_delayed_task_and_meets_day_16(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        base = draft()
        install = [
            option for option in base.catalogue if option.option_id == "INSTALL_SUPPORT"
        ]
        value = base.model_copy(
            update={
                "target_finish_offset_workdays": 16,
                "catalogue": install,
            }
        )
        revision = _approve(runtime, value, "run-install", "install")
        plan = revision["plan"]
        durations = _durations(plan)
        assert durations["C"] == 7, "the supplier delay must remain in the revision"
        assert durations["D"] == 3, "INSTALL_SUPPORT must reduce installation"
        project = ProjectInput.model_validate(plan)
        facts = evaluate_recovery(project, None, option_ids=[])
        assert facts.baseline.finish_offset_workdays == 16
        assert plan["version"] == 2
        assert plan["target_finish_offset_workdays"] == 16
        current = runtime.store.get_project_version("UTILITY-DEMO-001")
        assert current["version"] == 2
        assert current["project"]["version"] == 2
    finally:
        runtime.close()


def test_expedite_affecting_delayed_task_reproduces_finish_15(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        base = draft()
        expedite = [
            option for option in base.catalogue if option.option_id == "EXPEDITE_PANEL"
        ]
        value = base.model_copy(update={"catalogue": expedite})
        revision = _approve(runtime, value, "run-expedite", "expedite")
        durations = _durations(revision["plan"])
        assert durations["C"] == 5
        assert durations["D"] == 4
        facts = evaluate_recovery(
            ProjectInput.model_validate(revision["plan"]), None, option_ids=[]
        )
        assert facts.baseline.finish_offset_workdays == 15
    finally:
        runtime.close()


def test_multiple_selected_options_reproduce_finish_14(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        value = draft(
            target_finish_offset_workdays=14,
            budget_pence=400_000,
        )
        revision = _approve(runtime, value, "run-both", "both")
        durations = _durations(revision["plan"])
        assert durations["C"] == 5
        assert durations["D"] == 3
        facts = evaluate_recovery(
            ProjectInput.model_validate(revision["plan"]), None, option_ids=[]
        )
        assert facts.baseline.finish_offset_workdays == 14
    finally:
        runtime.close()


def test_eligible_zero_change_candidate_keeps_disrupted_plan(tmp_path):
    runtime = build_test_runtime(tmp_path)
    try:
        value = draft(
            target_finish_offset_workdays=17,
            budget_pence=0,
            catalogue=[],
        )
        revision = _approve(runtime, value, "run-nochange", "nochange")
        durations = _durations(revision["plan"])
        assert durations["C"] == 7
        facts = evaluate_recovery(
            ProjectInput.model_validate(revision["plan"]), None, option_ids=[]
        )
        assert facts.baseline.finish_offset_workdays == 17
    finally:
        runtime.close()
