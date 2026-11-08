# Business case and analyst assets

A synthetic regional team is upgrading a pump-control installation. A panel-supply delay threatens day 15 completion. The planner needs permitted mitigations; the manager needs costs, outcomes and assumptions before approving.

| Actor | Need | Authority |
|---|---|---|
| Planner | Assess disruption and prepare a decision pack | Submit/read authorized project runs |
| Manager | Approve/reject a current candidate | Commit an authorized version-bound decision |
| Reviewer | Explain the decision and recovery | Inspect synthetic facts, revision and audit evidence |

All organizations, activities, rates and permissions are synthetic. The framing is an illustrative hypothesis, not validated client discovery. No measured productivity or financial-return claim is made.

## Assumptions and boundary

Integer working-day offsets; finish-to-start, zero-lag dependencies. Catalogue reductions/costs are assumptions, not quotations. No invented options, dependency changes or hidden budget/deadline relaxation. Approval creates a local revision; it does not procure goods, allocate real crews or certify safe work.

## Success scenarios

| Scenario | Expected behavior |
|---|---|
| day 17 disruption; £2,000 budget; day 15 target | Expedite panel at £1,800 |
| Insufficient budget | Explicit no-feasible result; no relaxed constraints |
| Equal critical paths | Reducing one task need not reduce project finish |
| Missing/invalid input | Clarify or reject without invented facts |
| Wrong project or planner approval | Deny unauthorized persistence/mutation |
| Stale/competing candidate | Reject version/input/catalogue mismatch |
| Identical decision replay | Prior result; no duplicate revision |
| Provider failure/invalid narrative | Visible deterministic fallback/degraded reason |
| Pending approval at process restart | Recover wait and commit one decision |

Fixtures are under `tests/fixtures/delivery_recovery`. Candidate, contract, workflow, API, store and approval tests exercise these boundaries. The current acceptance matrix is in [engineering decisions](DECISIONS.md).

Delivered journey: evaluate → durable run → human decision → revision/audit → process recovery. Next: PostgreSQL test. Federation, multi-instance coordination, host-loss recovery and production operation require separate design and evidence.
