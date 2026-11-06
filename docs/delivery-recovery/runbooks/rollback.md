> Historical engineering reference, adapted for publication. Execution packets and approval states below belong to the original development sequence; they are not current authorization or a turnkey deployment guide. Start with [the current portfolio guide](/docs/portfolio/START_HERE.md). Personal paths and identifiers have been replaced; original byte-preserved records remain in the private audit archive.

# Rollback

Rollback is **operator-led**, not automatic. The only supported rollback path is
`service-control.sh rollback`, which removes the stopped container before
recreating it and preserves `/srv/delivery-recovery/data/current` and the
versioned `versions/` copies on the mounted filesystem.

All commands in this runbook execute **on the EC2 instance through SSM**, except
where explicitly marked as Windows/local.

## Preconditions

- Current failing image digest, source commit and smoke result recorded.
- Prior known-good digest exists in ECR and its source commit is recorded.
- Data backup is hash-verified; a schema-compatible prior digest is required.
- The data mount verifies: `service-control.sh verify`.

## Procedure (EC2 through SSM)

```bash
# 1. Confirm the current state and data mount.
/usr/local/lib/delivery-recovery/service-control.sh status
/usr/local/lib/delivery-recovery/service-control.sh verify

# 2. Roll back to the pinned prior digest. The helper stops, removes and
#    recreates; it does not leave a stopped container holding the name.
/usr/local/lib/delivery-recovery/service-control.sh rollback \
  <registry>/focused-delivery-recovery@sha256:<prior-digest>

# 3. Confirm the new image and readiness.
/usr/local/lib/delivery-recovery/service-control.sh status
curl -fsS http://127.0.0.1:8000/readyz
```

`service-control.sh rollback` updates `DELIVERY_RECOVERY_IMAGE` in
`/etc/delivery-recovery/service.env`, removes the old container, verifies the
data mount, and starts the service. It works for both Compose and docker-run
modes.

## Windows/local checks

Use SSM port forwarding from Windows, then run the smoke script locally against
`http://127.0.0.1:8000`. Do not run `docker stop`/`docker run` on Windows
against the EC2 daemon.

## Verify after rollback

- `/readyz` ready, schema version 2, worker running.
- Existing run IDs, revisions and audit events unchanged.
- Business smoke passes with exactly one revision.
- Data mount still resolves to the encrypted EBS volume.
- Rollback timeline and both digests recorded. No zero-downtime or automatic
  rollback claim is made.

## If the schema is incompatible

Stop and use `backup-restore.md` to restore a verified backup into a fresh
directory. Do not overwrite the only live copy. Record the restore time and
integrity result.
