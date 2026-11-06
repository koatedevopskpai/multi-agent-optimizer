> Historical engineering reference, adapted for publication. Execution packets and approval states below belong to the original development sequence; they are not current authorization or a turnkey deployment guide. Start with [the current portfolio guide](../../portfolio/START_HERE.md). Personal paths and identifiers have been replaced; original byte-preserved records remain in the private audit archive.

# Recreation for the interview

The interview is not scheduled. Recreate only after Koate requests a date and the
reviewed packet still matches current prices/prerequisites. The 32-hour compute
allowance includes an 8-hour interview deployment.

## 1. Rebuild the image

```powershell
git checkout <recorded-commit>
docker buildx build --platform linux/arm64 `
  --build-arg SOURCE_REVISION=<recorded-commit> `
  -t delivery-recovery:interview .
```

A rebuilt digest may differ; record it and re-run smoke/boundary checks. Do not
claim digest identity unless it actually matches.

## 2. Recreate infrastructure

Use explicit stage files and execute commands through the stage runner; do not
rely on `AWS_PROFILE` alone or leave `aws_profile="default"` in the common file:

- `terraform.bootstrap.tfvars`: `focused-bootstrap`, bootstrap role;
- `terraform.deploy.tfvars`: `focused-deploy`, deploy role;
- deploy-only OIDC runner: `terraform.deploy-oidc.tfvars` with `aws_profile=""`.

`expected_account_id` and `expected_stage_role_name` must both match. Run
`scripts/phase4_stage_exec.py` for each bootstrap/deploy/destroy command. It
sanitizes its child environment, verifies the actual assumed role and account,
then executes the command in that same environment. It does not change the
caller's shell; check `$LASTEXITCODE` after every wrapper call and stop on
failure.

Review the current plan and estimate. Confirm the single instance, encrypted
volumes, 7-day logs, no inbound/NAT/ELB/EIP, and Standard CPU-credit mode.

Push the pinned image, read the digest back from ECR, set `image_uri`, apply, and
verify the instance user data resolved the expected EBS volume before the
service starts.

## 3. Restore state

Use the protected local archive from `backup-restore.md`:

- use the manifest-only derivative for the unmodified `db4a072` exporter, or run
  the explicit `normalize-legacy` step and keep both hashes and the receipt;
- upload over SSM with `scripts/phase4_ssm_upload.py` (no S3);
- import/restore inside a one-off container on the encrypted data volume;
- switch the verified set with `restore-switch.sh` into `data/current` without
  renaming the mountpoint;
- verify hashes/integrity and run IDs;
- stop the service, swap the verified restored directory, `chown 10001:10001`,
  run `service-control.sh verify`, then start.

If the demonstration should start clean, seed the CP fixtures instead and label
them as fresh synthetic data. Do not publish the identity-bearing archives.

## 4. Verify before the interview

- `/readyz` ready, schema version 2, worker running, queue/pending sane.
- Deployed digest matches the container; source commit recorded.
- `service-control.sh verify` confirms the data mount source.
- Business smoke creates/approves one run with exactly one revision.
- CP states are reachable and run-bound history works.
- SSM forwarding works; no public app/SSH ingress.
- CloudWatch stream contains the smoke run ID.
- Backup/restore and rollback were rehearsed, not invented.

## 5. Cost and cleanup

- Standard CPU credits; no Unlimited surplus charge.
- Stop compute during pauses; inventory EBS/ECR/log/snapshot residual cost.
- Stop threshold £12; total ceiling £20 including the interview deployment.
- After the interview repeat `teardown.md`; do not leave idle project
  infrastructure during the waiting period.
