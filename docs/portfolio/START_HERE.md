# Engineering tour

1. [Business case](BUSINESS_CASE.md): two-day delay, permitted catalogue, deadline and accountable manager.
2. `src/multi_agent_optimizer/cpm.py` and `src/delivery_recovery/candidates.py`: deterministic computation and candidate policy.
3. `src/delivery_recovery/workflow/graph.py`, `providers.py` and `http_provider.py`: bounded orchestration, structured validation and fallback.
4. `src/delivery_recovery/approvals.py`, `storage/store.py` and `api/identity.py`: server authority, version binding and transactions.
5. [Evidence](QUALITY_AND_EVIDENCE.md), [architecture](ARCHITECTURE.md), [engineering decisions](DECISIONS.md) and [reference operations](../operations/REFERENCE_OPERATIONS.md).

Use the root README for reproducible local startup. The optimizer library remains under `src/multi_agent_optimizer`; the delivery-recovery application is the main vertical slice.
