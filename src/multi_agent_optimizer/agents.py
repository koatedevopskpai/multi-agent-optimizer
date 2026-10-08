"""Agents: deterministic-first, LLM-on-top, resilience on the edges.

Risk and optimisation outputs are fully deterministic — they come from the CPM
engine. LLM enrolment is optional and goes through a circuit-broken,
retry-with-backoff, Pydantic-validated call with a deterministic fallback
(see `resilience.py`).
"""

from __future__ import annotations

import os
import urllib.request
from typing import Protocol

from pydantic import BaseModel

from .cpm import Schedule
from .resilience import CircuitBreaker, safe_llm_json

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
    """Minimal OpenAI-compatible chat client (stdlib only)."""

    def __init__(self, model: str, base_url: str = "https://api.openai.com/v1", api_key: str = ""):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def chat(self, system: str, user: str) -> str:
        import json

        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.0,
                "response_format": {"type": "json_object"},
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
            import json as _json

            payload = _json.loads(response.read().decode("utf-8"))
        return payload["choices"][0]["message"]["content"]


def llm_from_env(prefix: str = "LLM") -> LLMClient:
    """Build a client from env. Empty provider => deterministic no-op mode."""
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


class Risk(BaseModel):
    task: str
    detail: str
    level: str  # HIGH | MED | LOW
    deterministic: bool = True


class Suggestion(BaseModel):
    task: str
    action: str
    expected_saving_days: int
    deterministic: bool = True


class _Notes(BaseModel):
    notes: str


class _Note(BaseModel):
    note: str


class _Thought(BaseModel):
    thought: str


class SchedulerAgent:
    def __init__(self, llm: LLMClient | None = None):
        self._llm = llm or NoopLLM()
        self._breaker = CircuitBreaker()

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
        enriched = safe_llm_json(
            self._llm,
            _Notes,
            "You summarise a CPM schedule into one JSON object {\"notes\": \"...\"}.",
            f"Project duration {schedule.project_duration} days.\n{table}",
            fallback=_Notes(notes=table),
            breaker=self._breaker,
        )
        return enriched.notes


class RiskAgent:
    def __init__(self, llm: LLMClient | None = None):
        self._llm = llm or NoopLLM()
        self._breaker = CircuitBreaker()

    def evaluate(self, schedule: Schedule) -> list[Risk]:
        risks: list[Risk] = []
        for task in schedule.tasks.values():
            if task.is_critical:
                risks.append(
                    Risk(task=task.id, detail="on the critical path: any delay extends the project", level="HIGH")
                )
            elif task.duration >= 5:
                risks.append(
                    Risk(
                        task=task.id,
                        detail=f"long task ({task.duration}d) with slack {task.slack}d: slippage eats buffer",
                        level="MED",
                    )
                )
        risks.sort(key=lambda r: {"HIGH": 0, "MED": 1, "LOW": 2}[r.level])

        if not isinstance(self._llm, NoopLLM):
            payload = ",".join(r.model_dump_json() for r in risks)
            note = safe_llm_json(
                self._llm,
                _Note,
                'You are a project risk reviewer. Reply JSON {"note": "..."}.',
                f"Risks: [{payload}]",
                fallback=_Note(note=""),
                breaker=self._breaker,
            )
            if note.note:
                risks.append(Risk(task="llm-note", detail=note.note, level="LOW", deterministic=False))
        return risks


class OptimiserAgent:
    def __init__(self, llm: LLMClient | None = None):
        self._llm = llm or NoopLLM()
        self._breaker = CircuitBreaker()

    def propose(self, schedule: Schedule) -> list[Suggestion]:
        suggestions: list[Suggestion] = []
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

        if not isinstance(self._llm, NoopLLM):
            payload = ",".join(s.model_dump_json() for s in suggestions)
            thought = safe_llm_json(
                self._llm,
                _Thought,
                'You are a schedule optimisation coach. Reply JSON {"thought": "..."}.',
                f"Suggestions: [{payload}]",
                fallback=_Thought(thought=""),
                breaker=self._breaker,
            )
            if thought.thought:
                suggestions.append(
                    Suggestion(task="llm-note", action=thought.thought, expected_saving_days=0, deterministic=False)
                )
        return suggestions