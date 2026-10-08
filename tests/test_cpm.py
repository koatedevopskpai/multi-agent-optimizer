import pytest

from multi_agent_optimizer import ProjectError, schedule

# A(3) -> B(2) -> D(2) \
#                \-> ...      F(3, deps D,E)
# A(3) -> C(4) -> E(1) -> .../
PROJECT = {
    "A": {"duration": 3},
    "B": {"duration": 2, "dependencies": ["A"]},
    "C": {"duration": 4, "dependencies": ["A"]},
    "D": {"duration": 2, "dependencies": ["B"]},
    "E": {"duration": 1, "dependencies": ["C"]},
    "F": {"duration": 3, "dependencies": ["D", "E"]},
}


def test_forward_and_backward_pass():
    plan = schedule(PROJECT)
    assert plan.project_duration == 11
    assert plan.tasks["A"].early_start == 0
    assert plan.tasks["A"].early_finish == 3
    assert plan.tasks["F"].early_finish == 11


def test_slack_and_critical_path():
    plan = schedule(PROJECT)
    assert plan.tasks["A"].slack == 0
    assert plan.tasks["B"].slack == 1
    assert plan.tasks["E"].slack == 0
    assert plan.tasks["F"].slack == 0
    assert sorted(plan.critical_path) == ["A", "C", "E", "F"]


def test_detects_cycle():
    bad = {
        "A": {"duration": 1, "dependencies": ["B"]},
        "B": {"duration": 1, "dependencies": ["A"]},
    }
    with pytest.raises(ProjectError, match="cycle"):
        schedule(bad)


def test_unknown_dependency():
    bad = {"A": {"duration": 1}, "B": {"duration": 1, "dependencies": ["Z"]}}
    with pytest.raises(ProjectError, match="unknown task"):
        schedule(bad)


def test_empty_project_rejected():
    with pytest.raises(ProjectError, match="no tasks"):
        schedule({})


def test_invalid_duration_rejected():
    with pytest.raises(ProjectError):
        schedule({"A": {"duration": -1}})


def test_zero_duration_is_allowed_and_critical():
    plan = schedule({"A": {"duration": 0}, "B": {"duration": 2, "dependencies": ["A"]}})
    assert plan.project_duration == 2
    assert plan.tasks["A"].slack == 0