"""Validation beyond the two original golden cases.

Covers dependency cycles, disconnected activities, zero and invalid
durations, multiple critical paths, impossible constraints and large valid
schedules.  Selected small graphs are checked against an independently
written longest-path baseline (memoised recursion over dependencies) rather
than against the engine's own forward/backward pass.
"""

from __future__ import annotations

import random

import pytest

from multi_agent_optimizer import MultiAgentOptimizer, NoopLLM, ProjectError, schedule


# ---------------------------------------------------------------------------
# Independent baseline: longest weighted path, computed by memoised recursion
# top-down.  Deliberately does not share code with cpm.schedule().
# ---------------------------------------------------------------------------


def _longest_ending_at(task_id: str, project: dict, memo: dict[str, int]) -> int:
    """Longest path length ending at ``task_id`` (its earliest finish)."""
    if task_id in memo:
        return memo[task_id]
    duration = project[task_id]["duration"]
    deps = project[task_id].get("dependencies", [])
    memo[task_id] = duration + max(
        (_longest_ending_at(dep, project, memo) for dep in deps), default=0
    )
    return memo[task_id]


def _longest_starting_at(
    task_id: str, project: dict, dependents: dict[str, list[str]], memo: dict[str, int]
) -> int:
    """Longest path length starting at ``task_id`` (its duration + longest tail)."""
    if task_id in memo:
        return memo[task_id]
    duration = project[task_id]["duration"]
    children = dependents.get(task_id, [])
    memo[task_id] = duration + max(
        (_longest_starting_at(child, project, dependents, memo) for child in children),
        default=0,
    )
    return memo[task_id]


def baseline(project: dict) -> dict[str, tuple[int, int, int]]:
    """Return ``{task: (early_finish, late_start, slack)}`` independently."""
    dependents: dict[str, list[str]] = {t: [] for t in project}
    for task, body in project.items():
        for dep in body.get("dependencies", []):
            dependents[dep].append(task)

    ends = {t: _longest_ending_at(t, project, {}) for t in project}
    project_end = max(ends.values())
    starts = {t: _longest_starting_at(t, project, dependents, {}) for t in project}

    result = {}
    for task in project:
        late_start = project_end - starts[task]
        early_finish = ends[task]
        result[task] = (
            early_finish,
            late_start,
            late_start - (early_finish - project[task]["duration"]),
        )
    return result


# ---------------------------------------------------------------------------
# Invariants every valid schedule must satisfy
# ---------------------------------------------------------------------------


def assert_invariants(project: dict, plan, start: int = 0) -> None:
    assert set(plan.tasks) == set(project)
    assert len(plan.topological_order) == len(project)
    assert set(plan.topological_order) == set(project)

    position = {task: i for i, task in enumerate(plan.topological_order)}
    for task, body in project.items():
        deps = body.get("dependencies", [])
        for dep in deps:
            assert position[dep] < position[task], (dep, task)

    for task, body in project.items():
        t = plan.tasks[task]
        assert t.early_start >= start
        assert t.early_finish == t.early_start + t.duration
        for dep in body.get("dependencies", []):
            assert t.early_start >= plan.tasks[dep].early_finish, (task, dep)
        assert t.late_finish == t.late_start + t.duration
        assert t.slack == t.late_start - t.early_start
        assert t.slack >= 0, task
        assert t.is_critical == (t.slack == 0)
        assert t.late_finish <= plan.project_duration
        for child, child_body in project.items():
            if task in child_body.get("dependencies", []):
                assert t.late_finish <= plan.tasks[child].late_start, (task, child)

    assert plan.project_duration == max(t.early_finish for t in plan.tasks.values())

    zero_slack = sorted(t for t, task in plan.tasks.items() if task.slack == 0)
    assert plan.critical_path == zero_slack  # sorted set, not an ordered path
    assert plan.critical_path, "a valid schedule always has at least one critical task"
    assert plan.tasks[plan.critical_path[0]].is_critical
    # The critical tasks span the whole project.
    assert min(plan.tasks[t].early_start for t in plan.critical_path) == start
    assert max(plan.tasks[t].early_finish for t in plan.critical_path) == (
        plan.project_duration
    )


# ---------------------------------------------------------------------------
# Independent baseline comparison
# ---------------------------------------------------------------------------

SMALL_EXAMPLES = {
    "diamond": {
        "A": {"duration": 3},
        "B": {"duration": 2, "dependencies": ["A"]},
        "C": {"duration": 4, "dependencies": ["A"]},
        "D": {"duration": 2, "dependencies": ["B"]},
        "E": {"duration": 1, "dependencies": ["C"]},
        "F": {"duration": 3, "dependencies": ["D", "E"]},
    },
    "two_disconnected_components": {
        "A": {"duration": 2},
        "B": {"duration": 5, "dependencies": ["A"]},
        "X": {"duration": 4},
        "Y": {"duration": 1, "dependencies": ["X"]},
    },
    "zero_durations": {
        "M": {"duration": 0},
        "N": {"duration": 0, "dependencies": ["M"]},
        "O": {"duration": 2, "dependencies": ["N"]},
    },
    "unequal_parallel_paths": {
        "S": {"duration": 1},
        "L": {"duration": 7, "dependencies": ["S"]},
        "R": {"duration": 2, "dependencies": ["S"]},
        "T": {"duration": 1, "dependencies": ["L", "R"]},
    },
}


@pytest.mark.parametrize("name", sorted(SMALL_EXAMPLES))
def test_matches_independent_baseline(name):
    project = SMALL_EXAMPLES[name]
    plan = schedule(project)
    expected = baseline(project)
    for task, (early_finish, late_start, slack) in expected.items():
        actual = plan.tasks[task]
        assert actual.early_finish == early_finish, task
        assert actual.late_start == late_start, task
        assert actual.slack == slack, task
    assert plan.project_duration == max(v[0] for v in expected.values())
    assert_invariants(project, plan)


def _random_dag(rng: random.Random, n: int) -> dict[str, dict]:
    order = [f"T{i}" for i in range(n)]
    project: dict[str, dict] = {}
    for i, task in enumerate(order):
        # Only depend on earlier tasks, so the graph is always acyclic.
        candidates = order[:i]
        deps = sorted(rng.sample(candidates, rng.randint(0, min(3, i))))
        project[task] = {
            "duration": rng.randint(0, 9),
            **({"dependencies": deps} if deps else {}),
        }
    return project


@pytest.mark.parametrize("seed", range(25))
def test_random_dags_match_independent_baseline(seed):
    rng = random.Random(seed)
    project = _random_dag(rng, rng.randint(1, 12))
    plan = schedule(project)
    expected = baseline(project)
    for task, (early_finish, late_start, slack) in expected.items():
        assert plan.tasks[task].early_finish == early_finish, (seed, task)
        assert plan.tasks[task].late_start == late_start, (seed, task)
        assert plan.tasks[task].slack == slack, (seed, task)
    assert plan.project_duration == max(v[0] for v in expected.values())
    assert_invariants(project, plan)


# ---------------------------------------------------------------------------
# Cycles and impossible constraints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "project",
    [
        {"A": {"duration": 1, "dependencies": ["B"]},
         "B": {"duration": 1, "dependencies": ["A"]}},
        {"A": {"duration": 1, "dependencies": ["A"]}},
        {"A": {"duration": 1, "dependencies": ["A2"]},
         "B": {"duration": 1, "dependencies": ["A"]},
         "C": {"duration": 1, "dependencies": ["B"]},
         "D": {"duration": 1, "dependencies": ["C"]},
         "E": {"duration": 1, "dependencies": ["D"]},
         "A2": {"duration": 1, "dependencies": ["E"]}},
    ],
)
def test_cycles_are_rejected(project):
    with pytest.raises(ProjectError, match="cycle"):
        schedule(project)


def test_cycle_error_names_the_cycle():
    project = {
        "A": {"duration": 1, "dependencies": ["C"]},
        "B": {"duration": 1, "dependencies": ["A"]},
        "C": {"duration": 1, "dependencies": ["B"]},
    }
    with pytest.raises(ProjectError) as excinfo:
        schedule(project)
    message = str(excinfo.value)
    assert "A" in message and "B" in message and "C" in message


def test_cycle_does_not_depend_on_insertion_order():
    for project in (
        {"A": {"duration": 1, "dependencies": ["B"]},
         "B": {"duration": 1, "dependencies": ["A"]}},
        {"B": {"duration": 1, "dependencies": ["A"]},
         "A": {"duration": 1, "dependencies": ["B"]}},
    ):
        with pytest.raises(ProjectError, match="cycle"):
            schedule(project)


def test_cycle_in_one_component_rejects_whole_project():
    project = {
        "A": {"duration": 1},
        "X": {"duration": 1, "dependencies": ["Y"]},
        "Y": {"duration": 1, "dependencies": ["X"]},
    }
    with pytest.raises(ProjectError, match="cycle"):
        schedule(project)


@pytest.mark.parametrize(
    "project, match",
    [
        ({}, "no tasks"),
        ({"A": {"duration": 1, "dependencies": ["Z"]}}, "unknown task"),
        ({"A": {"duration": 1, "dependencies": ["A", "A"]}}, "duplicate"),
        ({"A": {"duration": 1, "dependencies": [""]}}, "empty dependency"),
        ({"A": {}}, "missing required key"),
        ({"A": {"duration": -1}}, "non-negative int"),
        ({"A": {"duration": 1.5}}, "non-negative int"),
        ({"A": {"duration": "3"}}, "non-negative int"),
        ({"A": {"duration": None}}, "non-negative int"),
        ({"A": {"duration": True}}, "non-negative int"),
        ({"A": {"duration": 1, "dependencies": "B"}}, "list of task ids"),
        ({"A": {"duration": 1, "dependencies": [1]}}, "list of task ids"),
        ({"A": {"duration": 1}, "B": 3}, "must be a mapping"),
        ({"A": {"duration": 1}, "": {"duration": 1}}, "non-empty strings"),
        ([("A", {"duration": 1})], "mapping"),
        ("not a project", "mapping"),
    ],
)
def test_impossible_constraints_fail_explicitly(project, match):
    with pytest.raises(ProjectError, match=match):
        schedule(project)


@pytest.mark.parametrize("start", [-1, 1.5, "0", True])
def test_invalid_start_is_rejected(start):
    with pytest.raises(ProjectError, match="start"):
        schedule({"A": {"duration": 1}}, start=start)


# ---------------------------------------------------------------------------
# Structural cases called out by the task: disconnected, zero, multiple
# ---------------------------------------------------------------------------


def test_disconnected_activities_are_merged_into_one_schedule():
    project = SMALL_EXAMPLES["two_disconnected_components"]
    plan = schedule(project)
    assert plan.project_duration == 7
    assert plan.tasks["B"].slack == 0
    assert plan.tasks["Y"].slack == 2  # shorter component floats
    assert plan.tasks["X"].slack == 2
    assert plan.tasks["A"].slack == 0
    assert_invariants(project, plan)


def test_all_zero_durations_give_zero_length_project():
    project = {f"Z{i}": {"duration": 0} for i in range(4)}
    project["Z4"] = {"duration": 0, "dependencies": ["Z0", "Z1", "Z2", "Z3"]}
    plan = schedule(project)
    assert plan.project_duration == 0
    assert len(plan.critical_path) == 5
    assert_invariants(project, plan)


def test_zero_duration_milestone_on_a_critical_chain():
    project = {
        "A": {"duration": 2},
        "Milestone": {"duration": 0, "dependencies": ["A"]},
        "B": {"duration": 3, "dependencies": ["Milestone"]},
    }
    plan = schedule(project)
    assert plan.project_duration == 5
    assert plan.tasks["Milestone"].early_start == 2
    assert plan.tasks["Milestone"].is_critical
    assert_invariants(project, plan)


def test_equal_parallel_paths_are_both_critical():
    project = {
        "S": {"duration": 1},
        "P": {"duration": 3, "dependencies": ["S"]},
        "Q": {"duration": 3, "dependencies": ["S"]},
        "E": {"duration": 1, "dependencies": ["P", "Q"]},
    }
    plan = schedule(project)
    assert plan.critical_path == ["E", "P", "Q", "S"]
    assert plan.tasks["P"].slack == 0
    assert plan.tasks["Q"].slack == 0
    assert_invariants(project, plan)


def test_unequal_parallel_paths_leave_exactly_one_critical():
    project = SMALL_EXAMPLES["unequal_parallel_paths"]
    plan = schedule(project)
    assert plan.critical_path == ["L", "S", "T"]
    assert plan.tasks["R"].slack == 5
    assert_invariants(project, plan)


def test_multiple_disjoint_critical_paths():
    project = {
        "A": {"duration": 2},
        "B": {"duration": 2, "dependencies": ["A"]},
        "X": {"duration": 2},
        "Y": {"duration": 2, "dependencies": ["X"]},
    }
    plan = schedule(project)
    assert plan.critical_path == ["A", "B", "X", "Y"]
    assert plan.project_duration == 4
    assert_invariants(project, plan)


# ---------------------------------------------------------------------------
# Large valid schedules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reverse", [False, True])
def test_long_chain_does_not_hit_the_recursion_limit(reverse):
    n = 2000  # well past Python's default recursion limit of 1000
    indices = reversed(range(n)) if reverse else range(n)
    project: dict[str, dict] = {}
    for i in indices:
        body: dict = {"duration": 1}
        if i:
            body["dependencies"] = [f"T{i - 1}"]
        project[f"T{i}"] = body
    # Deepest-first insertion is the case a recursive DFS cannot survive.
    plan = schedule(project)
    assert plan.project_duration == n
    assert len(plan.critical_path) == n
    assert plan.topological_order == [f"T{i}" for i in range(n)]
    assert_invariants(project, plan)


def test_wide_schedule_with_one_long_tail():
    project: dict[str, dict] = {f"W{i}": {"duration": 1} for i in range(500)}
    project["W0"]["duration"] = 50
    plan = schedule(project)
    assert plan.project_duration == 50
    assert plan.critical_path == ["W0"]
    assert all(plan.tasks[f"W{i}"].slack == 49 for i in range(1, 500))
    assert_invariants(project, plan)


# ---------------------------------------------------------------------------
# Determinism and the model layer disabled
# ---------------------------------------------------------------------------


def test_schedule_is_deterministic_across_calls_and_insertion_orders():
    project = SMALL_EXAMPLES["diamond"]
    first = schedule(project)
    second = schedule(dict(reversed(list(project.items()))))
    assert first.project_duration == second.project_duration
    assert first.critical_path == second.critical_path
    for task in project:
        assert vars(first.tasks[task]) == vars(second.tasks[task])


def test_start_offset_shifts_every_task():
    project = SMALL_EXAMPLES["diamond"]
    plan = schedule(project, start=10)
    assert plan.project_duration == 21
    assert plan.tasks["A"].early_start == 10
    assert plan.tasks["F"].early_finish == 21
    assert plan.critical_path == ["A", "C", "E", "F"]
    assert_invariants(project, plan, start=10)


def test_invalid_project_is_never_partially_scheduled():
    project = {
        "A": {"duration": 1},
        "B": {"duration": 1, "dependencies": ["A"]},
        "C": {"duration": -5},
    }
    with pytest.raises(ProjectError):
        schedule(project)
    with pytest.raises(ProjectError):
        MultiAgentOptimizer(llm=NoopLLM()).optimize(project)


def test_model_layer_disabled_matches_the_deterministic_core():
    project = SMALL_EXAMPLES["diamond"]
    plan = schedule(project)
    result = MultiAgentOptimizer(llm=NoopLLM()).optimize(project)
    assert result.schedule.project_duration == plan.project_duration
    assert result.schedule.critical_path == plan.critical_path
    for task in project:
        assert vars(result.schedule.tasks[task]) == vars(plan.tasks[task])
    assert isinstance(result.narrative, str) and result.narrative
