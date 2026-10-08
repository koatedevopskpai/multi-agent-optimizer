# multi-agent-optimizer

**Deterministic-first project scheduling optimizer.** A unit-tested
**Critical Path Method (CPM) engine** decides; three LLM agents — **Scheduler,
Risk, Optimiser** — enrich and advise under an **Orchestrator**.

Core principle: the optimisation hot path is **pure, reproducible math**. LLMs
never touch the schedule calculation; they only add commentary, risk synthesis
and suggestions when a provider is configured. No keys, no network, zero
surprises by default.

## What it does

- **CPM engine** (`multi_agent_optimizer.cpm`): forward/backward pass,
  ES/EF/LS/LF, slack/float, critical path, with dependency validation and
  cycle detection. `schedule()` is deterministic and unit-tested.
- **RiskAgent**: flags critical-path tasks `HIGH`, long tasks `MED`.
- **OptimiserAgent**: proposes resource-adding moves on the critical path.
- **SchedulerAgent**: renders the annotated schedule.
- **Orchestrator** (`MultiAgentOptimizer.optimize()`): CPM first, agents over it.

## Quick start

```bash
pip install -e ".[dev]"
python -m pytest                      # unit tests
python -m multi_agent_optimizer.main  # run the worked example
python evals/evaluate.py              # golden-case eval gate
```

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
  golden_cases/     # ground-truth schedules + expected critical paths
  evaluate.py       # eval gate (blocks CI on mismatch)
tests/              # unit tests (pytest)
```

## Roadmap

- Vertex AI / Bedrock clients behind the same `LLMClient` protocol
- Optimiser proposals backed by a simple resource-leveling pass
- Web/API wrapper (FastAPI) + template for real deployments

## Licence

MIT — see [LICENSE](LICENSE). © 2026 Koate Kpai.