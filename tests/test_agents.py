from multi_agent_optimizer import MultiAgentOptimizer, schedule

PROJECT = {
    "A": {"duration": 3},
    "B": {"duration": 2, "dependencies": ["A"]},
    "C": {"duration": 4, "dependencies": ["A"]},
    "D": {"duration": 2, "dependencies": ["B"]},
    "E": {"duration": 1, "dependencies": ["C"]},
    "F": {"duration": 3, "dependencies": ["D", "E"]},
}


class FakeLLM:
    def chat(self, system: str, user: str) -> str:
        self.last = f"[fake] {system[:16]}...{user[:16]}"
        return self.last


def test_llm_enrichment_is_optional_and_non_blocking():
    optimizer = MultiAgentOptimizer(FakeLLM())
    result = optimizer.optimize(PROJECT)
    assert any(not r.deterministic for r in result.risks)
    assert any(not s.deterministic for s in result.suggestions)
    assert "fake" in result.narrative or "[fake]" in result.narrative


def test_noop_mode_runs_without_network_or_keys():
    # NoopLLM is the default; no API keys, no network.
    plan = schedule(PROJECT)
    assert plan.project_duration == 11
    result = MultiAgentOptimizer().optimize(PROJECT)
    assert result.schedule.project_duration == 11