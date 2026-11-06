# Database environment roadmap

Status: accepted roadmap; PostgreSQL implementation planned, not delivered.

Decision owner: Koate Kpai. Recorded: 4 November 2026.

## Decision

Use three database environment phases:

| Environment phase | Database | Delivery scope and status |
|---|---|---|
| Phase 1 — Development | SQLite | Current implemented development reference: application.db for application transactions and checkpoints.db for LangGraph checkpoints. |
| Phase 2 — Test | PostgreSQL | Will be implemented and verified as the next database environment stage. Not currently implemented or validated. |
| Phase 3 — Production | Not selected or deployed | Will not be implemented in this project because no production cluster is available. Production architecture may be discussed as a design; no production deployment or operational claims are made. |

These are **database environment phases**, separate from the existing numbered project delivery phases. In particular, the accepted project Phase 3 durability implementation remains SQLite, and project Phase 4 AWS preparation does not imply PostgreSQL or production readiness.

## Rationale and record integrity

SQLite is the chosen Phase 1 development database. It supports the current bounded single-instance implementation, local reproducibility and application transaction tests without requiring a separate database service.

PostgreSQL is the agreed database for Phase 2 test. It provides the next environment in which to implement and verify the application's persistence and concurrency behavior using a separate database service.

This document records the environment roadmap agreed on 4 November. It does not claim that a PostgreSQL alternatives assessment was documented before the original SQLite implementation. Existing SQLite evidence remains valid within its original scope.

## Phase 2 implementation and acceptance scope

Before describing PostgreSQL as delivered, implement and verify:

- PostgreSQL application persistence, schema migrations and LangGraph checkpoint integration.
- Existing authorization, version/catalogue binding, atomic decision/revision/audit behavior and idempotency guarantees.
- Job leasing and conflicting-operation behavior against real PostgreSQL, with bounded concurrency tests.
- Process restart recovery, pending human-decision persistence and connection/error handling.
- Backup and restore covering application data and checkpoints, with an explicit consistency strategy.
- Reproducible test-environment setup, configuration, secret handling, teardown and evidence identifying the actual database/environment used.

Database topology, hosting, migration of existing development data and detailed PostgreSQL infrastructure are not selected by this decision. Plan these before implementation. The current reviewed SQLite deployment baseline must not be silently replaced.

## Limits and authorization

PostgreSQL alone does not demonstrate high availability, scale-out or host/volume-loss recovery. Claim these only if the corresponding architecture is implemented and tested. Enterprise identity federation is a separate concern from the database.

This roadmap authorizes recording the intended development direction; it does not authorize new AWS resources or spending, alter the reviewed IAM inputs, or approve production deployment. The existing GBP20 gross budget, GBP12 operational stop threshold and current exclusions on non-IAM provisioning remain in force.
