# 45-minute technical review

Before the call, follow the root README. Check UI, `/readyz` and `/docs`. Keep local tokens private. The walkthrough needs no AWS restart or paid call. Use dated cloud images and retained receipts for the completed AWS experiment.

| Minutes | Demonstration | Engineering point |
|---|---|---|
| 0–5 | Panel delay, deadline and £2,000 budget | Business assumptions and outcome |
| 5–12 | Compare permitted recovery options | Exact facts, costs and rejection reasons |
| 12–20 | Durable run → manager approval → revision/audit | Human authority and transaction boundary |
| 20–26 | Separate run; stale-decision demonstration | Version/input/catalogue binding |
| 26–31 | Pending wait across local app restart | Checkpoints versus transient worker state |
| 31–36 | Provider/fallback tests and corrected LangSmith images | Untrusted model output; real/fake evidence distinction |
| 36–41 | Historical AWS recovery and cleanup | Operational evidence and bounded claims |
| 41–45 | PostgreSQL test, federation and resilience roadmap | Trade-offs and production gaps |

Use a fresh disposable local data directory if resetting the scenario; preserve previous data instead of deleting it during the call. For restart, stop normally and restart with the same `--data-dir`. `scripts/phase3_restart_drill.py --help` describes repeatable separate-process drills. The UI stale-action demo complements the transactional/concurrency tests; it does not replace them.

Code stops: `candidates.py`, `approvals.py`, `storage/store.py`, `workflow/graph.py`, `workflow/http_provider.py` and `scripts/phase4_restore_window.py`.

Closing explanation: the model makes a decision pack easier to understand; deterministic code establishes feasibility; an authorized human commits it. Process recovery and one bounded AWS restore path are demonstrated. Production identity, database topology and disaster recovery need additional work.
