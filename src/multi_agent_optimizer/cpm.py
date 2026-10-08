"""Deterministic Critical Path Method (CPM) engine.

No LLM in the hot path. ``schedule()`` is pure, unit-tested Project Management
math (forward/backward pass, float/slack, critical path, cycle + dependency
validation).

Task graph format::

    {
        "A": {"duration": 3},
        "B": {"duration": 2, "dependencies": ["A"]},
    }
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["ProjectError", "Task", "Schedule", "build_tasks", "topological_order", "schedule"]


class ProjectError(ValueError):
    """Raised for invalid project networks."""


@dataclass
class Task:
    id: str
    duration: int
    dependencies: list[str] = field(default_factory=list)
    early_start: int = 0
    early_finish: int = 0
    late_start: int = 0
    late_finish: int = 0
    slack: int = 0

    @property
    def is_critical(self) -> bool:
        return self.slack == 0


@dataclass
class Schedule:
    tasks: dict[str, Task]
    project_duration: int
    critical_path: list[str]
    topological_order: list[str]


def build_tasks(raw: dict[str, dict]) -> dict[str, Task]:
    """Validate and normalise a raw task dict into ``Task`` objects."""
    if not raw:
        raise ProjectError("project has no tasks")

    for key in raw:
        task = raw[key]
        if "duration" not in task:
            raise ProjectError(f"task {key!r} missing required key 'duration'")
        duration = task["duration"]
        if not isinstance(duration, int) or duration < 0:
            raise ProjectError(
                f"task {key!r} duration must be a non-negative int, got {duration!r}"
            )
        deps = task.get("dependencies", [])
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise ProjectError(f"task {key!r} dependencies must be a list of task ids")
        for dep in deps:
            if dep not in raw:
                raise ProjectError(f"task {key!r} depends on unknown task {dep!r}")

    return {
        key: Task(
            id=key,
            duration=raw[key]["duration"],
            dependencies=list(raw[key].get("dependencies", [])),
        )
        for key in raw
    }


def _detect_cycle(tasks: dict[str, Task]) -> list[str]:
    """Return a dependency cycle (as a path) if one exists, else empty."""
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {t: WHITE for t in tasks}
    path: list[str] = []

    def dfs(node: str) -> list[str]:
        colour[node] = GREY
        path.append(node)
        for dep in tasks[node].dependencies:
            if colour[dep] == GREY:
                start = path.index(dep)
                return path[start:] + [dep]
            if colour[dep] == WHITE:
                cycle = dfs(dep)
                if cycle:
                    return cycle
        path.pop()
        colour[node] = BLACK
        return []

    for node in tasks:
        if colour[node] == WHITE:
            cycle = dfs(node)
            if cycle:
                return cycle
    return []


def topological_order(tasks: dict[str, Task]) -> list[str]:
    """Kahn's algorithm; raises ``ProjectError`` on a cycle."""
    cycle = _detect_cycle(tasks)
    if cycle:
        raise ProjectError(f"dependency cycle detected: {' -> '.join(cycle)}")

    indegree = {t: 0 for t in tasks}
    dependents: dict[str, list[str]] = {t: [] for t in tasks}
    for task in tasks.values():
        for dep in task.dependencies:
            indegree[task.id] += 1
            dependents[dep].append(task.id)

    ready = [t for t, d in indegree.items() if d == 0]
    order: list[str] = []
    while ready:
        node = ready.pop()
        order.append(node)
        for child in dependents[node]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    return order


def schedule(raw: dict[str, dict], *, start: int = 0) -> Schedule:
    """Run the full CPM pass (forward, backward, slack, critical path)."""
    if not isinstance(start, int) or start < 0:
        raise ProjectError("'start' must be a non-negative int")

    tasks = build_tasks(raw)
    order = topological_order(tasks)

    # Forward pass: earliest start/finish.
    for node in order:
        es = start
        for dep in tasks[node].dependencies:
            es = max(es, tasks[dep].early_finish)
        tasks[node].early_start = es
        tasks[node].early_finish = es + tasks[node].duration

    project_end = max(t.early_finish for t in tasks.values())

    # Backward pass: latest start/finish + slack.
    for node in reversed(order):
        lf = project_end
        for child in [c for c in tasks if node in tasks[c].dependencies]:
            lf = min(lf, tasks[child].late_start)
        tasks[node].late_finish = lf
        tasks[node].late_start = lf - tasks[node].duration
        tasks[node].slack = tasks[node].late_start - tasks[node].early_start

    critical = sorted(t for t in tasks if tasks[t].is_critical)
    return Schedule(
        tasks=tasks,
        project_duration=project_end,
        critical_path=critical,
        topological_order=order,
    )