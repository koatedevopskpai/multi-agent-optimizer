> Historical engineering reference, adapted for publication. Execution packets and approval states below belong to the original development sequence; they are not current authorization or a turnkey deployment guide. Start with [the current portfolio guide](../portfolio/START_HERE.md). Personal paths and identifiers have been replaced; original byte-preserved records remain in the private audit archive.

# Phase 3 local runbook

Status: local reference implementation for durable jobs, authorised decisions,
audit and the React journey. No AWS deployment, paid model call or LangSmith
write is part of this runbook.

## Architecture

- One FastAPI process and one background `JobWorker`.
- Application transactions: SQLite `application.db`.
- LangGraph checkpoints: separate SQLite `checkpoints.db` via
  `langgraph-checkpoint-sqlite`.
- Data directory: `DELIVERY_RECOVERY_DATA_DIR` (default
  `var/delivery-recovery`).
- Queue: one active leased job, at most four ready-to-run queued jobs.
- Human waits (`needs_input`, `awaiting_approval`) release the lease and are
  retained separately with a 24-hour default expiry.
- Expired running leases are reclaimed on worker startup/poll. Pending human
  records survive a real process restart.
- Schema version 2 adds `project_versions`; migration 1 remains compatible.
- Provider: `LocalDemoProvider`, a deterministic fake provider labelled
  `fake_provider=True`; default tracing remains offline.

## Authorisation and version policy

- Run creation is authorised before persistence, queue writes or idempotency
  lookup. A concrete project must be in the server-derived principal scope.
- A project-less incomplete draft (`DRAFT-PROJECT`) requires wildcard project
  scope; it is otherwise denied with `PROJECT_FORBIDDEN`.
- Idempotency is scoped by principal and key. One principal cannot replay or
  disclose another principal's run with the same key.
- `project_versions` stores the authoritative current version, input hash,
  catalogue hash and project JSON per project.
- The authoritative tuple is version + input hash + catalogue hash. A run at an
  already established version must match all three; otherwise creation returns
  `PROJECT_VERSION_CONFLICT` before any run or job row is written.
- A catalogue price, duration or compatibility change is an explicit versioned
  change: it must use the next project version. The next version establishes the
  new authoritative tuple, skipped versions are rejected, and older versions are
  recorded only so the decision transaction can reject them as stale.
- An approval compares and advances that row in the same transaction as the
  decision, revision and audit event, including the catalogue hash in both the
  comparison and the compare-and-swap. Only one approval of a current tuple can
  win; another run bound to a prior version, input or catalogue receives
  `STALE_DECISION`.
- Rejection and idempotent replay do not advance the project version.
- Revisions are built from the disrupted plan plus the selected catalogue
  changes, not from baseline tasks. The persisted plan is re-evaluated before
  commit; a finish mismatch returns `REVISION_MISMATCH`.

## Start

```powershell
cd <USER_HOME>\development\multi-agent-optimizer
.\.venv-focused\Scripts\python.exe scripts\run_phase3_demo.py --port 8000
```

Open `http://127.0.0.1:8000/ui/`. OpenAPI is `http://127.0.0.1:8000/docs` and
the checked-in contract is `docs/delivery-recovery/openapi-phase3.json`.

The current R1-R4 capture instance uses the version-coherent fixture directory:

```powershell
.\.venv-focused\Scripts\python.exe scripts\run_phase3_demo.py `
  --port 8765 --data-dir var\delivery-recovery-r1r4
```

Its identity file is
`var/delivery-recovery-r1r4/demo-identities.json`; the default-path identity file
is not used by that instance.

## Demo identities

The server creates stable random bearer tokens on first start in:

`var/delivery-recovery/demo-identities.json`

That file is a local credential file and must stay outside evidence,
screenshots, URLs and public commits. It contains a planner and a manager
identity. Paste the tokens into the UI session fields; they are held in memory
only. Actor and roles are derived server-side; request actor/role fields and
unverified headers do not confer authority.

## API journey (SC-01)

1. `POST /v1/runs` with a planner bearer token and
   `{"draft": <DraftRecoveryRequest>}`. A successful submission is `202` and
   persisted before the response.
2. Poll `GET /v1/runs/{id}`. Expected path:
   `queued -> running -> awaiting_approval`.
3. The authoritative facts show baseline 15, disrupted 17, recommendation
   EXPEDITE_PANEL at finish 15 and GBP 1,800 within the GBP 2,000 budget.
4. `POST /v1/runs/{id}/decisions` with a manager token and an
   `Idempotency-Key`. Approval writes one decision, one revision and one audit
   event atomically, then queues checkpoint resume.
5. `GET /v1/runs/{id}/audit` shows the revision and event history.
6. Replaying the same key and content returns the prior decision/revision;
   different content is `IDEMPOTENCY_CONFLICT`; stale hashes/version are
   `STALE_DECISION`; a competing decision gets `DECISION_CONFLICT`.

## Restart drill

The drill runs `prepare` and `recover` as separate Python processes against one
file-backed data directory:

```powershell
.\.venv-focused\Scripts\python.exe scripts\phase3_restart_drill.py `
  prepare --mode approval --data C:\tmp\p3-approval
.\.venv-focused\Scripts\python.exe scripts\phase3_restart_drill.py `
  recover --mode approval --data C:\tmp\p3-approval
```

Modes:

- `queued`: queued work survives and executes after restart.
- `approval`: pending approval survives; recovery decides and commits once.
- `crash-window`: decision transaction commits before process exit; recovery
  finds the prior decision and advances the checkpoint without a duplicate
  revision or event.

The recovery observation starts at process launch. It includes Python startup
and is well below the 60-second pending-approval target on the recorded local
environment. This is process recovery on surviving local storage, not
host/volume-loss recovery.

## Test and evidence commands

```powershell
.\.venv-focused\Scripts\python.exe -m pytest
.\.venv-focused\Scripts\python.exe scripts\check_coverage.py `
  <coverage.json> `
  --require src/delivery_recovery/approvals.py=90 `
  --require src/delivery_recovery/storage/store.py=90 `
  --require src/delivery_recovery/api/app.py=90 `
  --require src/delivery_recovery/api/identity.py=90 `
  --require src/delivery_recovery/workflow/worker.py=90
.\.venv-focused\Scripts\python.exe evals/evaluate.py
cd web; npm test; npm run build
```

The R1-R4 correction probe replays wrong-project creation, two approvals of the
same project version and an INSTALL_SUPPORT-only revision:

```powershell
.\.venv-focused\Scripts\python.exe scripts\phase3_correction_probe.py
```

Set `DELIVERY_RECOVERY_SRC` to an older `src` tree to record the pre-fix
behaviour. The retained before/after outputs are in the
`DR-P3R-20261102T194000Z` evidence run.

## UI history binding

Audit history is bound to the loaded run, principal token and request
generation. Changing the run or identity clears history immediately; delayed
audit responses from a previous run are ignored. Pending runs show their current
creation/waiting events, and an approved completed run states that a manager
approved the committed revision rather than claiming no approval exists. The
explanation panel is labelled as a pre-decision recommendation snapshot; its
deterministic summary is written in the past tense and does not claim that no
decision was recorded.

## Known limits

- Single-instance SQLite; no multiple app replicas against one file.
- Tokens are a documented local demo mechanism, not enterprise federation.
- Model/provider outage does not disable deterministic calculations; the local
  provider is a fake and no paid provider is configured.
- No host/volume-loss or cloud recovery claim.
