"""Safe configuration tests for the opt-in live acceptance runner."""

from __future__ import annotations

from pathlib import Path

from scripts.run_deepseek_acceptance import load_secrets


def test_secret_loader_reads_only_allowed_keys_without_executing_values(tmp_path: Path):
    path = tmp_path / "secrets.env"
    path.write_text(
        "\n".join(
            [
                "DEEPSEEK_API_KEY='synthetic-deepseek'",
                "LANGSMITH_API_KEY=\"synthetic-langsmith\"",
                "OPENAI_API_KEY=must-not-load",
                "UNRELATED=$(echo should-not-execute)",
            ]
        ),
        encoding="utf-8",
    )

    values = load_secrets(path)

    assert values == {
        "DEEPSEEK_API_KEY": "synthetic-deepseek",
        "LANGSMITH_API_KEY": "synthetic-langsmith",
    }
    assert "OPENAI_API_KEY" not in values
    assert "UNRELATED" not in values
