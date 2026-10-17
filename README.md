# multi-agent-optimizer

[![CI](https://img.shields.io/github/actions/workflow/status/koatedevopskpai/multi-agent-optimizer/ci.yml?branch=main)](https://github.com/koatedevopskpai/multi-agent-optimizer/actions) [coverage ≥80% enforced in CI]

**Deterministic-first project scheduling optimizer.** A unit-tested
**Critical Path Method (CPM) engine** decides; three LLM agents — **Scheduler,
Risk, Optimiser** — enrich and advise under an **Orchestrator**.

Core principle: the optimisation hot path is **pure, reproducible math**. LLMs
never touch the schedule calculation; they only add commentary, risk synthesis
and suggestions when a provider is configured. No keys, no network, zero
surprises by default.

```
tasks ─▶ CPM engine (pure) ─▶ Schedule (ES/EF/LS/LF, slack, critical path)
                              │
        ┌─────────────────────┼──────────────────────┐
        ▼                     ▼                      ▼
  SchedulerAgent        RiskAgent()           OptimiserAgent()
    (order view)       (HIGH/MED risks)      (resource moves)
        │                     │                      │
        └───────── Orchestrator (MultiAgentOptimizer)
                              │
                     LLM enrichment (optional)
                circuit breaker + retry/backoff + Pydantic-validated
                → deterministic fallback on any failure
```

Everything on the engine side is unit-tested CPM; every LLM call is wrapped in
`resilience.py` (retry-with-backoff, circuit breaker, Pydantic v2 validation,
deterministic fallback).

## What it does

- **CPM engine** (`multi_agent_optimizer.cpm`): forward/backward pass,
  ES/EF/LS/LF, slack/float, critical path, with strict input validation and
  non-recursive cycle detection. Malformed projects (cycles, unknown or
  duplicate dependencies, negative/non-integer durations, mistyped task
  bodies) raise `ProjectError` instead of producing a schedule. `schedule()`
  is deterministic and unit-tested, including schedules with 2000 tasks.
- **RiskAgent**: flags critical-path tasks `HIGH`, long tasks `MED`.
- **OptimiserAgent**: proposes resource-adding moves on the critical path.
- **SchedulerAgent**: renders the annotated schedule.
- **Resilience** (`resilience.py`): retry-with-backoff, a circuit breaker, and
  Pydantic-v2-validated LLM calls that fall back to deterministic output when a
  provider fails or returns invalid JSON.
- **Orchestrator** (`MultiAgentOptimizer.optimize()`): CPM first, agents over it,
  LLM enrichment optional.

## Validation

`python evals/evaluate.py` runs 11 golden cases — 7 valid projects and 4
invalid ones that must raise `ProjectError`. Every valid case is checked for
its expected duration and zero-slack set, for dependency/timing invariants,
and against an independently written longest-path baseline. The report is
generated at `evals/EVAL_REPORT.md` (ignored, not committed).

`tests/test_cpm_validation.py` adds 25 seeded random DAGs against the same
baseline, cycles, disconnected components, zero durations, multiple critical
paths, malformed input, and schedules with 2000 tasks. Design, invariants and
known limits are in `docs/M01.md`.

## Quick start

```bash
pip install -e ".[dev,langgraph]"     # langgraph is needed for the coverage gate
python -m pytest                      # unit tests
python -m multi_agent_optimizer.main  # run the worked example
python evals/evaluate.py              # golden-case eval gate
```

The coverage gate is 80%. Installing only `.[dev]` leaves `lg_graph.py` and
part of `agents.py` unimported by the suite, so coverage falls below the gate
— the `langgraph` extra is not optional for a green local run.

### Using the API

```python
from multi_agent_optimizer import MultiAgentOptimizer

project = {
    "A": {"duration": 3},
    "B": {"duration": 2, "dependencies": ["A"]},
    "C": {"duration": 4, "dependencies": ["A"]},
    "D": {"duration": 2, "dependencies": ["B"]},
    "E": {"duration": 1, "dependencies": ["C"]},
    "F": {"duration": 3, "dependencies": ["D", "E"]},
}

result = MultiAgentOptimizer().optimize(project)
print(result.schedule.project_duration)   # 11
# critical_path is the sorted set of zero-slack task ids, not an ordered walk;
# with several critical paths, every task on any of them appears.
print(result.schedule.critical_path)      # ['A', 'C', 'E', 'F']
print(result.schedule.tasks["A"].slack)   # 0
print([r.task for r in result.risks if r.level == "HIGH"])   # ['A','C','E','F']
print(result.suggestions)
```

## LLM enrichment (optional)

Deterministic mode is the default (no `.env` needed). To enrich agent output:

```bash
# OpenAI-compatible (OpenAI, OpenRouter, vLLM, Ollama, Azure-compatible endpoints)
LLM_PROVIDER=openai LLM_MODEL=gpt-4o-mini LLM_API_KEY=sk-... LLM_BASE_URL=https://api.openai.com/v1
```

Set `LLM_PROVIDER=` (empty) to return to fully deterministic mode. **Vertex AI
provider is on the roadmap** — see `evals/` and the CI gate for how the quality
bar is enforced.

## Layout

```
src/multi_agent_optimizer/
  cpm.py            # deterministic CPM engine (no LLM)
  agents.py         # Scheduler, Risk, Optimiser + pluggable LLM client
  orchestrator.py   # MultiAgentOptimizer facade
evals/
  golden_cases/     # valid and invalid ground-truth cases
  evaluate.py       # eval gate (blocks CI on mismatch)
docs/
  M01.md            # validation design, invariants and limits
tests/              # unit tests (pytest), incl. test_cpm_validation.py
```

## LangGraph integration (optional)

The deterministic engine also ships as an **optional LangGraph `StateGraph`**
(`plan → risk → optimise`) — same design, orchestrated with LangGraph:

```bash
pip install -e ".[langgraph]"
```

```python
from multi_agent_optimizer.lg_graph import run_langgraph

state = run_langgraph(project)
print(state["duration"], state["critical_path"], state["risks"])
```

Without `langgraph` installed the core package still works; only
`lg_graph.build_graph()` requires the extra.

## Roadmap

- Vertex AI / Bedrock clients behind the same `LLMClient` protocol
- Optimiser proposals backed by a simple resource-leveling pass
- Web/API wrapper (FastAPI) + template for real deployments

## Licence

MIT — see [LICENSE](LICENSE). © 2026 Koate Kpai.