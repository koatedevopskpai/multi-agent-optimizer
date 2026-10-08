"""Agents: deterministic-first, LLM-on-top.

The risk and optimisation outputs are fully deterministic — they come from the
CPM engine. The LLM is an optional enrichment layer (configurable provider,
default is a no-op so everything runs without API keys).
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .cpm import Schedule

__all__ = [
    "LLMClient",
    "NoopLLM",
    "OpenAICompatibleLLM",
    "llm_from_env",
    "Risk",
    "Suggestion",
    "SchedulerAgent",
    "RiskAgent",
    "OptimiserAgent",
]


class LLMClient(Protocol):
    def chat(self, system: str, user: str) -> str: ...


class NoopLLM:
    """Deterministic fallback: no LLM, no surprises, no keys."""

    def chat(self, system: str, user: str) -> str:
        return ""


class OpenAICompatibleLLM:
    """Minimal OpenAI-compatible chat client (stdlib only).

    Works with OpenAI, OpenRouter, vLLM, Ollama and Azure-OpenAI-compatible
    endpoints via ``base_url``/``model``.
    """

    def __init__(self, model: str, base_url: str = "https://api.openai.com/v1", api_key: str = ""):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def chat(self, system: str, user: str) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.0,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload["choices"][0]["message"]["content"]


def llm_from_env(prefix: str = "LLM") -> LLMClient:
    """Build a client from env. Empty provider => deterministic no-op mode.

    Providers: openai / openrouter / vllm / ollama / azure-compatible (all use
    the OpenAI chat API shape). `vertex` raises until wired (see README).
    """
    provider = os.getenv(f"{prefix}_PROVIDER", "").strip().lower()
    if not provider:
        return NoopLLM()
    if provider == "vertex":
        raise RuntimeError(
            "Vertex AI provider is not wired yet; set LLM_PROVIDER=openai (or leave empty for deterministic mode)"
        )
    return OpenAICompatibleLLM(
        model=os.getenv(f"{prefix}_MODEL", "gpt-4o-mini"),
        base_url=os.getenv(f"{prefix}_BASE_URL", "https://api.openai.com/v1"),
        api_key=os.getenv(f"{prefix}_API_KEY", ""),
    )


@dataclass
class Risk:
    task: str
    detail: str
    level: str  # HIGH | MED | LOW
    deterministic: bool = True


@dataclass
class Suggestion:
    task: str
    action: str
    expected_saving_days: int
    deterministic: bool = True


class SchedulerAgent:
    """Owns the schedule view: early/late times, slack, critical path."""

    def __init__(self, llm: LLMClient | None = None):
        self._llm = llm or NoopLLM()

    def run(self, schedule: Schedule) -> str:
        rows = [
            (
                f"{t}  es={schedule.tasks[t].early_start}  "
                f"ef={schedule.tasks[t].early_finish}  slack={schedule.tasks[t].slack}"
                + ("  CRITICAL" if schedule.tasks[t].is_critical else "")
            )
            for t in schedule.topological_order
        ]
        table = "\n".join(rows)
        if isinstance(self._llm, NoopLLM):
            return table
        return self._llm.chat(
            "You summarise a CPM schedule into 2-3 plain-English bullets.",
            f"Project duration {schedule.project_duration} days.\n{table}",
        )


class RiskAgent:
    """Deterministic risk view, LLM commentary optional."""

    def __init__(self, llm: LLMClient | None = None):
        self._llm = llm or NoopLLM()

    def evaluate(self, schedule: Schedule) -> list[Risk]:
        risks = []
        for task in schedule.tasks.values():
            if task.is_critical:
                risks.append(
                    Risk(task.id, "on the critical path: any delay extends the project", "HIGH")
                )
            elif task.duration >= 5:
                risks.append(
                    Risk(
                        task.id,
                        f"long task ({task.duration}d) with slack {task.slack}d: slippage eats buffer",
                        "MED",
                    )
                )
        risks.sort(key=lambda r: {"HIGH": 0, "MED": 1, "LOW": 2}[r.level])
        if isinstance(self._llm, NoopLLM):
            return risks
        summary = self._llm.chat(
            "You are a project risk reviewer. One short paragraph.",
            "Risks: " + json.dumps([r.__dict__ for r in risks]),
        )
        risks.append(Risk("llm-note", summary, "LOW", deterministic=False))
        return risks


class OptimiserAgent:
    """Proposes schedule improvements. Deterministic heuristic: shorten the
    critical path by adding resources to critical tasks."""

    def __init__(self, llm: LLMClient | None = None):
        self._llm = llm or NoopLLM()

    def propose(self, schedule: Schedule) -> list[Suggestion]:
        suggestions = []
        for task in schedule.tasks.values():
            if not task.is_critical or task.duration <= 1:
                continue
            saving = min(task.duration - 1, task.duration // 2)
            suggestions.append(
                Suggestion(
                    task=task.id,
                    action=f"add resources to shorten '{task.id}'",
                    expected_saving_days=saving,
                )
            )
        if isinstance(self._llm, NoopLLM):
            return suggestions
        thoughts = self._llm.chat(
            "You are a schedule optimisation coach. Short advice, no code.",
            "Suggestions: " + json.dumps([s.__dict__ for s in suggestions]),
        )
        suggestions.append(
            Suggestion("llm-note", thoughts, 0, deterministic=False)
        )
        return suggestions