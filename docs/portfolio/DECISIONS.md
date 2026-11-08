# Engineering decisions

## Deterministic scheduling, model explanation

The CPM engine computes schedule facts. Catalogue policy validates permitted changes and costs. LangGraph organizes intake, computation, explanation and human waits. The model cannot authorize a user, relax a constraint or commit a revision. Deterministic fallback keeps valid facts available if explanation fails.

## Explicit provider boundary

The HTTP adapter owns structured tool messages, retries, transport deadlines and usage admission. LangGraph is native; project-owned provider contracts make budget and failure behavior explicit. Controlled fake-provider tests establish malformed-output handling. Historical live-provider traces establish only the specific recorded integration behavior. A native model SDK is an alternative if it preserves these bounded contracts; no superiority claim is made without comparison.

## SQLite development; PostgreSQL test next

SQLite provides a reproducible single-instance development topology with application transactions and separate graph checkpoints. PostgreSQL is planned for the test environment, requiring migrations, real concurrency checks, checkpoint integration and a consistent backup strategy. It is not implemented. There is no production cluster in this project.

Application and graph stores are not a distributed transaction. Reconciliation, idempotency and version binding address their boundary; backup requires quiescing writes to capture a consistent recovery unit. Process recovery on surviving storage is distinct from host/volume-loss recovery or HA.

## Synthetic acceptance scenarios

| Requirement | Expected behavior | Verification area |
|---|---|---|
| Exact feasible recovery | Day17 disruption can recover to day15 for £1,800 within £2,000 | Candidate/contract tests and golden fixtures |
| No hidden relaxation | Infeasible budget/deadline remains rejected | Policy and parallel-path tests |
| Scoped authority | Wrong project/role cannot read or mutate another run | API identity and approvals |
| Current decision only | Version/input/catalogue mismatch is rejected | CAS and catalogue-binding tests |
| Replay protection | Identical decision returns its original outcome | Idempotency and store tests |
| Durable human wait | Pending decision survives process restart | Separate-process restart drills |
| Provider degradation | Invalid narrative yields visible fallback | Workflow/provider/trace tests |

These requirements describe the delivered reference behavior, not evidence of stakeholder discovery, measured productivity improvement or production readiness.
