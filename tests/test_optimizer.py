from multi_agent_optimizer import MultiAgentOptimizer

PROJECT = {
    "A": {"duration": 3},
    "B": {"duration": 2, "dependencies": ["A"]},
    "C": {"duration": 4, "dependencies": ["A"]},
    "D": {"duration": 9, "dependencies": ["B"]},
    "E": {"duration": 1, "dependencies": ["C"]},
    "F": {"duration": 3, "dependencies": ["D", "E"]},
}

# A -> B(5) is deliberately not the critical branch: A -> C -> D -> E is 9
# days, so B has slack and is medium risk for its length, not high risk.
MEDIUM_PROJECT = {
    "A": {"duration": 1},
    "B": {"duration": 5, "dependencies": ["A"]},
    "C": {"duration": 3, "dependencies": ["A"]},
    "D": {"duration": 4, "dependencies": ["C"]},
    "E": {"duration": 1, "dependencies": ["B", "D"]},
}


def test_end_to_end_deterministic():
    result = MultiAgentOptimizer().optimize(PROJECT)
    assert result.schedule.project_duration == 17
    assert result.schedule.critical_path == ["A", "B", "D", "F"]
    assert result.narrative  # schedule table is always present in no-op mode


def test_risks_flag_critical_tasks_high():
    result = MultiAgentOptimizer().optimize(PROJECT)
    critical_ids = set(result.schedule.critical_path)
    high = {r.task for r in result.risks if r.level == "HIGH"}
    assert high == critical_ids == {"A", "B", "D", "F"}


def test_suggestions_target_critical_tasks():
    result = MultiAgentOptimizer().optimize(PROJECT)
    critical_ids = set(result.schedule.critical_path)
    for s in result.suggestions:
        if s.deterministic:
            assert s.task in critical_ids
            assert s.expected_saving_days > 0


def test_long_noncritical_task_is_medium_risk():
    result = MultiAgentOptimizer().optimize(MEDIUM_PROJECT)
    levels = {r.task: r.level for r in result.risks}
    assert result.schedule.tasks["B"].slack > 0
    assert levels["B"] == "MED"
    assert levels["A"] == levels["C"] == levels["D"] == levels["E"] == "HIGH"
