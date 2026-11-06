"""Phase 2 bounded workflow package.

The deterministic domain remains authoritative. This package adds a typed
conditional graph, allowlisted read-only tools, a provider boundary and local
trace instrumentation. Persistent approval is deliberately not implemented.
"""

from .contracts import (
    ClarificationRequest,
    DraftRecoveryRequest,
    Explanation,
    ProviderOutcome,
    WorkflowError,
    WorkflowResult,
)
from .exporters import LangSmithClientSink, LangSmithTraceExporter
from .http_provider import OpenAICompatibleProvider
from .providers import FakeProvider, ProviderRunner
from .service import RecoveryWorkflow, WorkflowLimits
from .tools import TOOL_SPECS, ToolError, execute_tool

__all__ = [
    "ClarificationRequest",
    "DraftRecoveryRequest",
    "Explanation",
    "FakeProvider",
    "LangSmithClientSink",
    "LangSmithTraceExporter",
    "OpenAICompatibleProvider",
    "ProviderOutcome",
    "ProviderRunner",
    "RecoveryWorkflow",
    "WorkflowError",
    "WorkflowLimits",
    "WorkflowResult",
    "TOOL_SPECS",
    "ToolError",
    "execute_tool",
]
