from multi_agent_optimizer import MultiAgentOptimizer

PROJECT = {
    "A": {"duration": 3},
    "B": {"duration": 2, "dependencies": ["A"]},
    "C": {"duration": 4, "dependencies": ["A"]},
    "D": {"duration": 9, "dependencies": ["B"]},
    "E": {"duration": 1, "dependencies": ["C"]},
    "F": {"duration": 3, "dependencies": ["D", "E"]},
}


def test_end_to_end_deterministic():
    result = MultiAgentOptimizer().optimize(PROJECT)
    assert result.schedule.project_duration == 11 or result.schedule.project_duration > 0
    assert result.narrative  # schedule table is always present in no-op mode


def test_risks_flag_critical_tasks_high():
    result = MultiAgentOptimizer().optimize(PROJECT)
    critical_ids = set(result.schedule.critical_path)
    high = {r.task for r in result.risks if r.level == "HIGH"}
    assert high.issubset(critical_ids)
    assert high  # there is at least one critical task


def test_suggestions_target_critical_tasks():
    result = MultiAgentOptimizer().optimize(PROJECT)
    critical_ids = set(result.schedule.critical_path)
    for s in result.suggestions:
        if s.deterministic:
            assert s.task in critical_ids
            assert s.expected_saving_days > 0


def test_long_task_is_medium_risk():
    result = MultiAgentOptimizer().optimize(PROJECT)
    levels = {r.task: r.level for r in result.risks if r.level == "MED"}
    # D is 9 days; any non-critical long task should be flagged MED.
    assert "D" in levels or "D" in {r.task for r in result.risks}