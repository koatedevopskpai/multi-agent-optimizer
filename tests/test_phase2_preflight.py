"""The environment probe is a configuration preflight, not a live capability check."""

from __future__ import annotations

from scripts.probe_phase2_capabilities import main


def test_preflight_is_labeled_and_never_claims_auth_or_delivery(monkeypatch, capsys):
    for key in (
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LANGSMITH_API_KEY",
        "LANGSMITH_ENDPOINT",
        "LANGSMITH_TRACING",
        "LANGSMITH_PROJECT",
    ):
        monkeypatch.delenv(key, raising=False)

    assert main() == 0
    output = capsys.readouterr().out

    assert "mode=configuration_preflight_only" in output
    assert "provider_configuration=missing" in output
    assert "langsmith_configuration=missing" in output
    assert "checks_authentication=false" in output
    assert "checks_trace_delivery=false" in output
    assert "network_calls=0" in output
