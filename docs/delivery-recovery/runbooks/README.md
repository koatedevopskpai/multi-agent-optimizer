> Historical engineering reference, adapted for publication. Execution packets and approval states below belong to the original development sequence; they are not current authorization or a turnkey deployment guide. Start with [the current portfolio guide](/docs/portfolio/START_HERE.md). Personal paths and identifiers have been replaced; original byte-preserved records remain in the private audit archive.

# Phase 4 runbooks

These runbooks are **prepared, not executed** against AWS. Commands use the
approved profile `default`, region `eu-west-2`, project tag
`focused-delivery-recovery` and placeholders in angle brackets. Never paste
credentials or account IDs into evidence, URLs, screenshots or chat.

| Runbook | Use |
|---|---|
| [deploy.md](deploy.md) | Build/push the pinned image and apply the reviewed stack |
| [smoke.md](smoke.md) | Health, business smoke and log correlation |
| [failure-recovery.md](failure-recovery.md) | Process kill/restart, faulty release, bounded failure evidence |
| [rollback.md](rollback.md) | Operator-led return to a prior pinned digest |
| [backup-restore.md](backup-restore.md) | Consistent app/checkpoint backup and fresh-environment restore |
| [pause-resume.md](pause-resume.md) | Stop chargeable compute during pauses and resume |
| [teardown.md](teardown.md) | Remove project resources and verify no residual cost |
| [recreation.md](recreation.md) | Rebuild and recreate the stack for the interview |

Related: `PHASE4_IAM_MATRIX.md` defines deployer/operator/runtime scopes and
`infra/bootstrap/` contains the tested disk resolver, service-control helper,
backup exporter and systemd unit.
