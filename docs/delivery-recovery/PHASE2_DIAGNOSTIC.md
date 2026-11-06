> Historical engineering reference, adapted for publication. Execution packets and approval states below belong to the original development sequence; they are not current authorization or a turnkey deployment guide. Start with [the current portfolio guide](/docs/portfolio/START_HERE.md). Personal paths and identifiers have been replaced; original byte-preserved records remain in the private audit archive.

# Phase 2 diagnostic: schema-invalid model output

Status: controlled fake-provider exercise, 2 November 2026. This is not a production incident and does not establish live-model behaviour.

## Setup

- Scenario: synthetic utilities recovery, baseline 15, disrupted 17, budget GBP 2,000.
- Provider: `FakeProvider` returning invalid JSON twice.
- Workflow: typed LangGraph intake → compute → assistant → validate/fallback.
- Source revision and exact run: recorded in the run manifest under `evidence/delivery-recovery/runs/DR-P2-*`.
- Network calls: zero. No provider credentials or LangSmith export were used.

## Symptom and competing hypotheses

The workflow completed its CPM calculation but did not produce a model explanation.

Hypotheses considered before reading the trace:

1. Transport timeout or connection failure.
2. Authentication or throttling failure.
3. Schema-invalid model output.
4. Semantically invalid but schema-valid output.

## Observed trace

Observed span order for the controlled run:

```
intake → compute → assistant → provider_call:1 → provider_call:2 → fallback → finalize
```

Observed result: `status=degraded`, `provider_calls=2`, `reason=INVALID_MODEL_OUTPUT`.

The transport call succeeded both times, so transport/auth/throttle were not the cause. The assistant received a response but `Explanation.model_validate_json` rejected it; one deliberate repair was attempted; the second response was still invalid; the workflow fell back to the deterministic explanation. The domain evaluation remained available with baseline 15 and disrupted 17.

## Root cause

Schema-invalid provider output, with the single repair budget exhausted. This is an injected fault, not an observed production failure.

## Fix and regression

The behaviour is intentional and bounded: one deliberate repair, then a labelled deterministic fallback. The regression is `test_schema_invalid_output_repairs_once_then_falls_back` in `tests/test_phase2_workflow.py`.

The semantic variant is covered separately by `test_semantic_invalid_facts_repair_then_fall_back`, whose trace includes `validation` spans with error counts.

## What the trace does and does not establish

- It establishes local span parent/child instrumentation, attempt counts and fallback reason for the fake provider.
- It does not establish a live LangSmith export, real-provider latency, model quality or a production incident.
- Fake-provider traces remain labelled `fake_provider=true` in trace metadata.
