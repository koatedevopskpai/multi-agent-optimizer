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
    """Validate and normalise a raw task dict into ``Task`` objects.

    Raises:
        ProjectError: for an empty project, unnamed tasks, a non-mapping task
            definition, a missing/negative/non-integer duration, malformed or
            duplicated dependencies, or a dependency on an unknown task.
    """
    if not isinstance(raw, dict):
        raise ProjectError("project must be a mapping of task ids to task definitions")
    if not raw:
        raise ProjectError("project has no tasks")

    for key in raw:
        task = raw[key]
        if not isinstance(key, str) or not key.strip():
            raise ProjectError(f"task ids must be non-empty strings, got {key!r}")
        if not isinstance(task, dict):
            raise ProjectError(
                f"task {key!r} must be a mapping, got {type(task).__name__}"
            )
        if "duration" not in task:
            raise ProjectError(f"task {key!r} missing required key 'duration'")
        duration = task["duration"]
        # bool is an int subclass; True is not a duration.
        if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
            raise ProjectError(
                f"task {key!r} duration must be a non-negative int, got {duration!r}"
            )
        deps = task.get("dependencies", [])
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise ProjectError(f"task {key!r} dependencies must be a list of task ids")
        if len(set(deps)) != len(deps):
            raise ProjectError(f"task {key!r} has duplicate dependencies")
        for dep in deps:
            if not dep:
                raise ProjectError(f"task {key!r} has an empty dependency id")
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
    """Return a dependency cycle (as a path) if one exists, else empty.

    Iterative rather than recursive: a valid schedule may contain thousands
    of chained tasks, and a recursive DFS hits Python's recursion limit when
    the chain is encountered deepest-first.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {t: WHITE for t in tasks}

    for root in tasks:
        if colour[root] != WHITE:
            continue
        colour[root] = GREY
        path: list[str] = [root]
        # Each frame is (node, index of the next dependency to inspect).
        stack: list[tuple[str, int]] = [(root, 0)]
        while stack:
            node, index = stack[-1]
            deps = tasks[node].dependencies
            if index >= len(deps):
                colour[node] = BLACK
                stack.pop()
                path.pop()
                continue
            child = deps[index]
            stack[-1] = (node, index + 1)
            if colour[child] == GREY:
                start = path.index(child)
                return path[start:] + [child]
            if colour[child] == WHITE:
                colour[child] = GREY
                path.append(child)
                stack.append((child, 0))
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
    if len(order) != len(tasks):
        # Defensive: _detect_cycle above should have caught this.
        raise ProjectError("dependency cycle detected")
    return order


def schedule(raw: dict[str, dict], *, start: int = 0) -> Schedule:
    """Run the full CPM pass (forward, backward, slack, critical path)."""
    if isinstance(start, bool) or not isinstance(start, int) or start < 0:
        raise ProjectError("'start' must be a non-negative int")

    tasks = build_tasks(raw)
    order = topological_order(tasks)

    dependents: dict[str, list[str]] = {t: [] for t in tasks}
    for task in tasks.values():
        for dep in task.dependencies:
            dependents[dep].append(task.id)

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
        for child in dependents[node]:
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