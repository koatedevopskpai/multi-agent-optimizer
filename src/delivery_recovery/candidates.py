"""Deterministic recovery comparison above the existing CPM engine.

Authoritative facts come from a full schedule recomputation. The catalogue is
small enough for exhaustive enumeration: at most four options, sixteen
combinations. No model is consulted here.
"""

from __future__ import annotations

import hashlib
import json
from itertools import combinations
from typing import Any, Iterable

from multi_agent_optimizer.cpm import schedule as cpm_schedule

from .contracts import (
    CandidateResult,
    Disruption,
    PolicyError,
    ProjectInput,
    RecoveryEvaluation,
    ScheduleFacts,
    TaskTiming,
)

__all__ = ["evaluate_recovery"]


def _hash_payload(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _project_payload(project: ProjectInput) -> dict[str, Any]:
    payload = project.model_dump(mode="json", exclude={"catalogue"})
    payload["tasks"] = sorted(
        (
            {
                **task,
                "dependency_ids": sorted(task["dependency_ids"]),
            }
            for task in payload["tasks"]
        ),
        key=lambda task: task["id"],
    )
    return payload


def _catalogue_payload(project: ProjectInput) -> list[dict[str, Any]]:
    options = []
    for option in sorted(project.catalogue, key=lambda item: item.option_id):
        payload = option.model_dump(mode="json")
        payload["changes"] = sorted(payload["changes"], key=lambda change: change["task_id"])
        payload["incompatible_option_ids"] = sorted(payload["incompatible_option_ids"])
        options.append(payload)
    return options


def _baseline_durations(project: ProjectInput) -> dict[str, int]:
    return {task.id: task.duration_workdays for task in project.tasks}


def _task_graph(project: ProjectInput, durations: dict[str, int]) -> dict[str, dict]:
    return {
        task.id: {
            "duration": durations[task.id],
            "dependencies": list(task.dependency_ids),
        }
        for task in project.tasks
    }


def _schedule_facts(project: ProjectInput, durations: dict[str, int]) -> ScheduleFacts:
    plan = cpm_schedule(_task_graph(project, durations), start=project.start_offset_workdays)
    input_hash = _hash_payload(
        {
            "project": _project_payload(project),
            "durations": {task_id: durations[task_id] for task_id in sorted(durations)},
        }
    )
    return ScheduleFacts(
        input_hash=input_hash,
        start_offset_workdays=project.start_offset_workdays,
        finish_offset_workdays=plan.project_duration,
        elapsed_workdays=plan.project_duration - project.start_offset_workdays,
        critical_task_ids=list(plan.critical_path),
        task_timings={
            task_id: TaskTiming(
                early_start=task.early_start,
                early_finish=task.early_finish,
                slack=task.slack,
            )
            for task_id, task in plan.tasks.items()
        },
    )


def _candidate_id(base_hash: str, catalogue_hash: str, selection: tuple[str, ...]) -> str:
    """Return a stable, unambiguous identifier independent of catalogue order.

    The ID is a versioned hash of the canonical selection and the plan/catalogue
    hashes. Delimiter characters in option IDs cannot make two selections
    collide because the option list is hashed as JSON, not concatenated.
    """

    payload = {
        "schema": "candidate-id-v1",
        "base_hash": base_hash,
        "catalogue_hash": catalogue_hash,
        "option_ids": list(selection),
    }
    return "CAND-" + _hash_payload(payload)


def _selection_policy_error(
    project: ProjectInput,
    option_ids: Iterable[str],
) -> PolicyError | None:
    """Return the first policy violation for a selection, or ``None``.

    This is the single compatibility policy used by both explicit and
    enumerated evaluation. Incompatibility is symmetric: a declaration on
    either option forbids the pair.
    """

    requested = list(option_ids)
    if len(set(requested)) != len(requested):
        return PolicyError("DUPLICATE_OPTION", "an option may be selected only once")

    catalogue = {option.option_id: option for option in project.catalogue}
    unknown = sorted(set(requested) - set(catalogue))
    if unknown:
        return PolicyError("UNKNOWN_OPTION", f"unknown catalogue option(s): {unknown}")

    selection = tuple(sorted(requested))
    selected = [catalogue[option_id] for option_id in selection]
    for index, left in enumerate(selected):
        for right in selected[index + 1 :]:
            if (
                right.option_id in left.incompatible_option_ids
                or left.option_id in right.incompatible_option_ids
            ):
                return PolicyError(
                    "INCOMPATIBLE_OPTIONS",
                    f"options {left.option_id!r} and {right.option_id!r} are incompatible",
                )

    seen_tasks: set[str] = set()
    for option in selected:
        for change in option.changes:
            if change.task_id in seen_tasks:
                return PolicyError(
                    "OVERLAPPING_OPTIONS",
                    f"selected options change task {change.task_id!r} more than once",
                )
            seen_tasks.add(change.task_id)
    return None


def _evaluate_selection(
    project: ProjectInput,
    disrupted_durations: dict[str, int],
    disrupted_facts: ScheduleFacts,
    catalogue_hash: str,
    selection: tuple[str, ...],
) -> CandidateResult:
    error = _selection_policy_error(project, selection)
    if error is not None:
        raise error

    catalogue = {option.option_id: option for option in project.catalogue}
    selection = tuple(sorted(selection))
    candidate_durations = dict(disrupted_durations)
    task_reductions: dict[str, int] = {}
    cost_pence = 0

    for option_id in selection:
        option = catalogue[option_id]
        cost_pence += option.incremental_cost_pence
        for change in option.changes:
            current = candidate_durations[change.task_id]
            if change.new_duration_workdays >= current:
                raise PolicyError(
                    "NON_REDUCTION",
                    f"option {option_id!r} changes {change.task_id!r} from "
                    f"{current} to {change.new_duration_workdays}; a catalogue change "
                    "must reduce the disrupted duration",
                )
            candidate_durations[change.task_id] = change.new_duration_workdays
            task_reductions[change.task_id] = task_reductions.get(change.task_id, 0) + (
                current - change.new_duration_workdays
            )

    candidate_facts = _schedule_facts(project, candidate_durations)
    project_saving = disrupted_facts.finish_offset_workdays - candidate_facts.finish_offset_workdays

    rejection_codes: list[str] = []
    if candidate_facts.finish_offset_workdays > project.target_finish_offset_workdays:
        rejection_codes.append("TARGET_MISSED")
    if cost_pence > project.budget_pence:
        rejection_codes.append("BUDGET_EXCEEDED")

    return CandidateResult(
        candidate_id=_candidate_id(disrupted_facts.input_hash, catalogue_hash, selection),
        base_hash=disrupted_facts.input_hash,
        catalogue_hash=catalogue_hash,
        option_ids=list(selection),
        task_reductions=dict(sorted(task_reductions.items())),
        project_saving_workdays=project_saving,
        finish_offset_workdays=candidate_facts.finish_offset_workdays,
        cost_pence=cost_pence,
        feasible=not rejection_codes,
        rejection_codes=rejection_codes,
    )


def evaluate_recovery(
    project: ProjectInput,
    disruption: Disruption | None = None,
    *,
    option_ids: list[str] | None = None,
) -> RecoveryEvaluation:
    """Evaluate catalogue options against a copied, disrupted plan.

    Args:
        project: Strict, validated baseline project and permitted catalogue.
        disruption: Optional confirmed duration change. The baseline is never
            mutated.
        option_ids: Evaluate one explicit selection. ``None`` enumerates every
            catalogue combination; ``[]`` evaluates the no-change candidate.
    """

    baseline_durations = _baseline_durations(project)
    disrupted_durations = dict(baseline_durations)
    if disruption is not None:
        if disruption.task_id not in disrupted_durations:
            raise PolicyError(
                "UNKNOWN_TASK",
                f"disruption names unknown task {disruption.task_id!r}",
            )
        disrupted_durations[disruption.task_id] = disruption.revised_duration_workdays

    baseline_facts = _schedule_facts(project, baseline_durations)
    disrupted_facts = _schedule_facts(project, disrupted_durations)
    catalogue_hash = _hash_payload(_catalogue_payload(project))

    if option_ids is None:
        catalogue_ids = sorted(option.option_id for option in project.catalogue)
        selections = [
            tuple(combo)
            for size in range(len(catalogue_ids) + 1)
            for combo in combinations(catalogue_ids, size)
            if _selection_policy_error(project, combo) is None
        ]
    else:
        selections = [tuple(sorted(option_ids))]

    candidates = [
        _evaluate_selection(
            project,
            disrupted_durations,
            disrupted_facts,
            catalogue_hash,
            selection,
        )
        for selection in selections
    ]

    ranked_feasible = sorted(
        (candidate for candidate in candidates if candidate.feasible),
        key=lambda candidate: (
            candidate.cost_pence,
            candidate.finish_offset_workdays,
            candidate.candidate_id,
        ),
    )
    recommended = ranked_feasible[0].candidate_id if ranked_feasible else None

    return RecoveryEvaluation(
        baseline=baseline_facts,
        disrupted=disrupted_facts,
        candidates=candidates,
        recommended_candidate_id=recommended,
        ranked_feasible_candidate_ids=[candidate.candidate_id for candidate in ranked_feasible],
    )
