"""Eval gate: run the CPM engine against golden cases and assert correctness.

Each case is either a valid project (with an expected duration and critical
path) or an invalid one (with an ``expected_error`` substring).  Valid cases
are additionally checked against scheduling invariants and against an
independently written longest-path baseline.

Usage:  python evals/evaluate.py
Exit code 0 on pass, 1 on any mismatch. Writes evals/EVAL_REPORT.md.
"""

from __future__ import annotations

import json
import pathlib
import sys

from multi_agent_optimizer import ProjectError, schedule

REPO = pathlib.Path(__file__).resolve().parents[1]
GOLDEN_DIR = pathlib.Path(__file__).parent / "golden_cases"
REPORT = pathlib.Path(__file__).parent / "EVAL_REPORT.md"


# ---------------------------------------------------------------------------
# Independent baseline (memoised longest weighted paths; no CPM passes).
# ---------------------------------------------------------------------------


def _earliest_finish(task: str, project: dict, memo: dict[str, int]) -> int:
    if task not in memo:
        deps = project[task].get("dependencies", [])
        memo[task] = project[task]["duration"] + max(
            (_earliest_finish(dep, project, memo) for dep in deps), default=0
        )
    return memo[task]


def _longest_tail(
    task: str, project: dict, dependents: dict[str, list[str]], memo: dict[str, int]
) -> int:
    if task not in memo:
        children = dependents.get(task, [])
        memo[task] = project[task]["duration"] + max(
            (_longest_tail(c, project, dependents, memo) for c in children), default=0
        )
    return memo[task]


def baseline(project: dict, start: int = 0) -> dict[str, tuple[int, int, int]]:
    dependents: dict[str, list[str]] = {t: [] for t in project}
    for task, body in project.items():
        for dep in body.get("dependencies", []):
            dependents[dep].append(task)
    ends = {t: _earliest_finish(t, project, {}) for t in project}
    tails = {t: _longest_tail(t, project, dependents, {}) for t in project}
    project_end = max(ends.values())
    return {
        t: (
            ends[t] + start,
            project_end - tails[t] + start,
            (project_end - tails[t]) - (ends[t] - project[t]["duration"]),
        )
        for t in project
    }


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


def invariant_errors(project: dict, plan, start: int = 0) -> list[str]:
    errors: list[str] = []
    if set(plan.tasks) != set(project):
        errors.append("task set differs from the project")
        return errors
    if len(plan.topological_order) != len(project) or set(plan.topological_order) != set(project):
        errors.append("topological order is not a permutation of the tasks")

    position = {task: i for i, task in enumerate(plan.topological_order)}
    for task, body in project.items():
        entry = plan.tasks[task]
        deps = body.get("dependencies", [])
        for dep in deps:
            if position.get(dep, -1) >= position.get(task, -1):
                errors.append(f"{task} is not scheduled after {dep}")
        if entry.early_start < start:
            errors.append(f"{task} starts before the project start")
        if entry.early_finish != entry.early_start + entry.duration:
            errors.append(f"{task} early_finish != early_start + duration")
        if entry.late_finish != entry.late_start + entry.duration:
            errors.append(f"{task} late_finish != late_start + duration")
        if entry.slack != entry.late_start - entry.early_start:
            errors.append(f"{task} slack != late_start - early_start")
        if entry.slack < 0:
            errors.append(f"{task} has negative slack")
        if (entry.slack == 0) != entry.is_critical:
            errors.append(f"{task} is_critical disagrees with slack")

    if plan.project_duration != max(t.early_finish for t in plan.tasks.values()):
        errors.append("project_duration is not the maximum early finish")
    if plan.critical_path != sorted(
        t for t, entry in plan.tasks.items() if entry.slack == 0
    ):
        errors.append("critical_path is not the sorted set of zero-slack tasks")
    return errors


def _baseline_errors(project: dict, plan, start: int = 0) -> list[str]:
    errors: list[str] = []
    expected = baseline(project, start)
    for task, (early_finish, late_start, slack) in expected.items():
        entry = plan.tasks[task]
        if entry.early_finish != early_finish:
            errors.append(f"{task} early_finish {entry.early_finish} != baseline {early_finish}")
        if entry.late_start != late_start:
            errors.append(f"{task} late_start {entry.late_start} != baseline {late_start}")
        if entry.slack != slack:
            errors.append(f"{task} slack {entry.slack} != baseline {slack}")
    expected_end = max(v[0] for v in expected.values())
    if plan.project_duration != expected_end:
        errors.append(f"project_duration {plan.project_duration} != baseline {expected_end}")
    return errors


# ---------------------------------------------------------------------------


def _format_critical(ids: list[str]) -> str:
    if len(ids) > 8:
        return f"{len(ids)} tasks"
    return ",".join(ids)


def main() -> int:
    failures: list[str] = []
    rows: list[str] = []

    for path in sorted(GOLDEN_DIR.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        name = case["name"]
        project = case["project"]
        start = case.get("start", 0)
        expected_error = case.get("expected_error")

        try:
            plan = schedule(project, start=start)
            raised: ProjectError | None = None
        except ProjectError as exc:
            plan = None
            raised = exc

        if expected_error is not None:
            ok = raised is not None and expected_error in str(raised)
            rows.append(
                f"| {name} | invalid | {expected_error} | "
                f"{str(raised) if raised is not None else 'no error'} | {'PASS' if ok else 'FAIL'} |"
            )
            if not ok:
                if raised is not None:
                    failures.append(
                        f"{path.name}: expected ProjectError containing "
                        f"{expected_error!r}, got {raised!r}"
                    )
                else:
                    failures.append(
                        f"{path.name}: expected ProjectError containing "
                        f"{expected_error!r}, but the project was accepted"
                    )
            continue

        if plan is None:
            rows.append(f"| {name} | valid | ok | {raised} | FAIL |")
            failures.append(f"{path.name}: engine raised on a valid case: {raised}")
            continue

        expected = case["expected"]
        actual_duration = plan.project_duration
        actual_critical = sorted(plan.critical_path)
        exp_duration = expected["project_duration"]
        exp_critical = sorted(expected["critical_path"])

        errors = invariant_errors(project, plan, start)
        errors += _baseline_errors(project, plan, start)

        ok = (
            actual_duration == exp_duration
            and actual_critical == exp_critical
            and not errors
        )
        rows.append(
            f"| {name} | valid | {exp_duration}d / {_format_critical(exp_critical)} | "
            f"{actual_duration}d / {_format_critical(actual_critical)} | "
            f"{'PASS' if ok else 'FAIL'} |"
        )
        if actual_duration != exp_duration:
            failures.append(
                f"{path.name}: expected duration {exp_duration} got {actual_duration}"
            )
        if actual_critical != exp_critical:
            failures.append(
                f"{path.name}: expected critical {exp_critical} got {actual_critical}"
            )
        failures.extend(f"{path.name}: {error}" for error in errors)

    lines = [
        "# Evaluation report",
        "",
        f"_Generated by `python evals/evaluate.py` — deterministic CPM, no model calls. "
        f"{len(rows)} case(s)._",
        "",
        "| Case | Kind | Expected | Actual | Result |",
        "|---|---|---|---|---|",
        *rows,
        "",
        *([] if failures else ["**All golden cases pass; invariants and baseline agree.**"]),
    ]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if failures:
        print("EVAL FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"ALL golden cases pass ({len(rows)} cases). Report saved -> {REPORT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
