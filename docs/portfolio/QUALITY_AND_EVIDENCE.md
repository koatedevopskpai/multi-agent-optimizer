# Quality requirements and evidence

| Concern | Acceptance behavior | Evidence and limit |
|---|---|---|
| Correctness | Deterministic candidate recomputation | CPM/oracle/golden/policy tests; 11 retained golden cases |
| Authorization | Server-derived role/project checks | API/identity/approval tests; local bearer reference |
| Consistency | Version/input/catalogue binding; atomic revision/audit | CAS/idempotency/store tests; single application DB |
| Persistence | Durable jobs and pending human decisions | Subprocess restart drills; surviving local storage |
| Provider resilience | Reject unsafe narrative; visible fallback | Provider/graph tests and controlled trace |
| Bounded work | Leasing, queue admission, graph/provider budgets | Tests/configuration; no production capacity claim |
| Recoverability | Restore/switch/verify/restart/reverify/rollback | Historical native AWS execution and retained private receipts |
| Cleanup | Remove scoped billables; retain local assets | Accepted 5 November 2026 cleanup; IAM/access roles excluded |
| Privacy | Individual image review with exact hashes | 12 selected from 37 approved derivatives; no raw originals |

## Visual evidence

- [Manager approval](images/CP-05-committed.jpg), [stale rejection](images/CP-05-stale-conflict.jpg), [recovered wait](images/CP-05-recovered-pending.jpg): synthetic local workflow.
- [Successful LangSmith root](images/CP-02-root.png) and [usage](images/CP-02-usage.png): historical real-provider run, subsequently re-exported with corrected trace metadata. Not a new execution or measured bill.
- [Invalid-output span](images/CP-03-validation.png): controlled fake provider, not attributed to a live model fault.
- [AWS restart HTTP verification](images/CP07_CLOUDWATCH_RESTART_VERIFICATION.jpg), [terminal readiness](images/CP07_CLOUDWATCH_TERMINAL_READINESS.jpg), [stopped state](images/CP07_FINAL_EC2_STOPPED.jpg), [encrypted disks](images/CP07_FINAL_ENCRYPTED_DISKS.jpg): historical pre-cleanup views. HTTP200 alone does not prove DB contents; companion SSM/API receipts establish those checks.

## Attributed historical results

Three recorded local restart observations were approximately 1.5–1.6 seconds; they are not a latency distribution. The accepted AWS restore/switch/restart/terminal-rollback execution took 176.835 seconds across 12 successful SSM commands, verifying one run, one approved revision and four audit events. Final cleanup's remaining three deletions completed successfully, with recorded absence checks and retained-asset hashes independently reconciled.

Current automated verification is recorded in [VALIDATION.md](VALIDATION.md). Raw logs, state and execution receipts are not part of the public evidence set; screenshots alone do not establish every underlying logical assertion.

## Not established

Frozen holdout evaluation, production load profile, PostgreSQL, enterprise SSO, scale-out, HA, host/volume-loss recovery, spontaneous crash and AWS recreation. Historical container scans had residual findings: this is not a current clean security attestation.
