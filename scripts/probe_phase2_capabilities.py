"""Phase 2 configuration preflight.

This is not an authentication, model-capability, workspace-addressing or
trace-delivery probe. It only reports whether relevant environment variables
are present. It never prints secret values and never contacts a provider or
LangSmith.
"""

from __future__ import annotations

import os

KEYS = (
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LANGSMITH_API_KEY",
    "LANGSMITH_ENDPOINT",
    "LANGSMITH_TRACING",
    "LANGSMITH_PROJECT",
)


def main() -> int:
    print("mode=configuration_preflight_only")
    for key in KEYS:
        value = os.getenv(key)
        print(f"{key}={'set' if value else 'missing'}")
    provider_configured = bool(os.getenv("LLM_PROVIDER") and os.getenv("LLM_API_KEY"))
    langsmith_configured = bool(os.getenv("LANGSMITH_API_KEY"))
    print(f"provider_configuration={'present' if provider_configured else 'missing'}")
    print(f"langsmith_configuration={'present' if langsmith_configured else 'missing'}")
    print("checks_authentication=false")
    print("checks_model_capability=false")
    print("checks_workspace_addressing=false")
    print("checks_trace_delivery=false")
    print("network_calls=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
