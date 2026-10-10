import pytest

pytest.importorskip("langgraph")

from multi_agent_optimizer.lg_graph import run_langgraph  # noqa: E402

PROJECT = {
    "A": {"duration": 3},
    "B": {"duration": 2, "dependencies": ["A"]},
    "C": {"duration": 4, "dependencies": ["A"]},
    "D": {"duration": 2, "dependencies": ["B"]},
    "E": {"duration": 1, "dependencies": ["C"]},
    "F": {"duration": 3, "dependencies": ["D", "E"]},
}


def test_langgraph_pipeline_end_to_end():
    state = run_langgraph(PROJECT)
    assert state["duration"] == 11
    assert state["critical_path"]
    assert state["narrative"]
    assert any(r["level"] == "HIGH" for r in state["risks"])
    assert isinstance(state["suggestions"], list)
