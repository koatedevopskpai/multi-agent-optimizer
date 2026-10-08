"""Orchestrator: CPM engine + Scheduler/Risk/Optimiser agents."""

from __future__ import annotations

from dataclasses import dataclass

from .agents import LLMClient, NoopLLM, OptimiserAgent, Risk, RiskAgent, SchedulerAgent, Suggestion
from .cpm import Schedule, schedule

__all__ = ["OptimizerResult", "MultiAgentOptimizer"]


@dataclass
class OptimizerResult:
    schedule: Schedule
    risks: list[Risk]
    suggestions: list[Suggestion]
    narrative: str


class MultiAgentOptimizer:
    """Deterministic-first: CPM decides, agents enrich and advise."""

    def __init__(self, llm: LLMClient | None = None):
        self._llm = llm or NoopLLM()
        self.scheduler = SchedulerAgent(self._llm)
        self.risk_agent = RiskAgent(self._llm)
        self.optimiser = OptimiserAgent(self._llm)

    def optimize(self, tasks: dict[str, dict]) -> OptimizerResult:
        plan = schedule(tasks)
        risks = self.risk_agent.evaluate(plan)
        suggestions = self.optimiser.propose(plan)
        narrative = self.scheduler.run(plan)
        return OptimizerResult(
            schedule=plan,
            risks=risks,
            suggestions=suggestions,
            narrative=narrative,
        )