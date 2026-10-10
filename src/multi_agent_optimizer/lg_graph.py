"""Optional LangGraph orchestration over the deterministic CPM engine.

This is a thin, optional layer: the same deterministic-first design as the rest
of the package, expressed as a LangGraph ``StateGraph`` (plan -> risk -> optimise).
If ``langgraph`` isn't installed, importing this module is safe but building the
graph raises a clear error.

Install:  pip install -e ".[langgraph]"
"""

from __future__ import annotations

from typing import TypedDict

from .agents import LLMClient, OptimiserAgent, RiskAgent, SchedulerAgent
from .cpm import schedule

try:  # optional dependency
    from langgraph.graph import END, StateGraph

    _LANGGRAPH = True
except ImportError:  # pragma: no cover - exercised only without the extra
    END = None
    StateGraph = None
    _LANGGRAPH = False

__all__ = ["OptimizerState", "build_graph", "run_langgraph"]


class OptimizerState(TypedDict, total=False):
    tasks: dict
    duration: int
    critical_path: list[str]
    narrative: str
    risks: list[dict]
    suggestions: list[dict]


def _require_langgraph() -> None:
    if not _LANGGRAPH:
        raise RuntimeError(
            "langgraph is not installed — run: pip install -e '.[langgraph]'"
        )


def build_graph(llm: LLMClient | None = None):
    """Compile a LangGraph ``StateGraph``: plan -> risk -> optimise."""
    _require_langgraph()

    scheduler = SchedulerAgent(llm)
    risk_agent = RiskAgent(llm)
    optimiser = OptimiserAgent(llm)

    def plan_node(state: OptimizerState) -> OptimizerState:
        plan = schedule(state["tasks"])
        state["duration"] = plan.project_duration
        state["critical_path"] = plan.critical_path
        state["narrative"] = scheduler.run(plan)
        return state

    def risk_node(state: OptimizerState) -> OptimizerState:
        plan = schedule(state["tasks"])
        state["risks"] = [r.model_dump() for r in risk_agent.evaluate(plan)]
        return state

    def optimise_node(state: OptimizerState) -> OptimizerState:
        plan = schedule(state["tasks"])
        state["suggestions"] = [s.model_dump() for s in optimiser.propose(plan)]
        return state

    graph = StateGraph(OptimizerState)
    graph.add_node("plan", plan_node)
    graph.add_node("risk", risk_node)
    graph.add_node("optimise", optimise_node)
    graph.set_entry_point("plan")
    graph.add_edge("plan", "risk")
    graph.add_edge("risk", "optimise")
    graph.add_edge("optimise", END)
    return graph.compile()


def run_langgraph(tasks: dict, llm: LLMClient | None = None) -> OptimizerState:
    """Run the LangGraph pipeline once and return the final state."""
    app = build_graph(llm)
    return app.invoke({"tasks": tasks})
