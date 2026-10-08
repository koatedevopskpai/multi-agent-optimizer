"""multi-agent-optimizer: deterministic-first project scheduling optimizer."""

from .cpm import ProjectError, Schedule, Task, build_tasks, schedule, topological_order
from .orchestrator import MultiAgentOptimizer, OptimizerResult
from .agents import (
    LLMClient,
    NoopLLM,
    OpenAICompatibleLLM,
    Risk,
    Suggestion,
    llm_from_env,
)

__all__ = [
    "ProjectError",
    "Schedule",
    "Task",
    "build_tasks",
    "schedule",
    "topological_order",
    "MultiAgentOptimizer",
    "OptimizerResult",
    "LLMClient",
    "NoopLLM",
    "OpenAICompatibleLLM",
    "Risk",
    "Suggestion",
    "llm_from_env",
]

__version__ = "0.1.0"