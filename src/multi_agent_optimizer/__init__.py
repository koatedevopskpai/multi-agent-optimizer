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

from .resilience import (
    CircuitBreaker,
    CircuitOpenError,
    extract_json_object,
    parse_validated,
    retry_with_backoff,
    safe_llm_json,
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
    "CircuitBreaker",
    "CircuitOpenError",
    "retry_with_backoff",
    "parse_validated",
    "safe_llm_json",
    "extract_json_object",
]

__version__ = "0.1.0"