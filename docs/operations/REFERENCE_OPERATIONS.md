# Reference operations

Local usage is in the root README. The cloud templates illustrate a single-instance ARM64 application, encrypted data volume, registry and logs. They do not create a running environment by themselves. The recorded deployment was torn down; this guide is a generic engineering reference.

## Identity and execution guards

Separate bootstrap, deploy, operator and runtime roles as described in [IAM roles](../../infra/iam/README.md). Render real-environment inputs into gitignored `var/phase4`, never into public docs. Configure the expected account and expected role for each stage. Use `scripts/phase4_stage_exec.py` to verify identity and execute the operation in the same child process; a separate identity check does not establish the later process's identity.

For local named profiles, derive `terraform.bootstrap.tfvars` and `terraform.deploy.tfvars` from the corresponding examples, and keep their environment-specific values private. A PowerShell wrapper must stop on native failures: `if ($LASTEXITCODE -ne 0) { throw 'Stage failed' }`. Do not substitute a default administrator identity when a scoped operation is denied.

## Image identity

After selecting the intended registry, build a local image and use a unique immutable tag. The following illustrates ordering only; `$Registry`, `$Tag` and region must be supplied for the intended environment. Registry authentication, upload authority and scoped stage identity must already be established.

```powershell
docker tag delivery-recovery:local "${Registry}/focused-delivery-recovery:${Tag}"
if ($LASTEXITCODE -ne 0) { throw 'Tag failed' }
docker push "${Registry}/focused-delivery-recovery:${Tag}"
if ($LASTEXITCODE -ne 0) { throw 'Push failed' }
aws ecr describe-images --repository-name focused-delivery-recovery --image-ids imageTag=$Tag --query 'imageDetails[0].imageDigest'
if ($LASTEXITCODE -ne 0) { throw 'Digest readback failed' }
```

Assess the actual image scan, platform and digest before generating a deployment plan. A tag alone is insufficient image identity. The source Dockerfile's historical pins are not a current vulnerability clearance.

## Deploy and verify

Render placeholders, inspect the exact IAM documents, select network topology, check the saved Terraform plan and define the operation/teardown window before cloud execution. Check actual readiness, application run/revision/audit behavior and correlated logs after deployment. Inspectable code and reference tests are not evidence of a new live deployment. The repository's CI performs no cloud deployment.

## Consistent backup and restore

The recovery unit comprises application data, graph checkpoints and their manifest/inventory. Quiesce application writes and verify the stopped state before backing up both stores. `infra/bootstrap/backup-export.sh` fails closed if service stop or mount verification fails. Keep raw archives private; validate hashes and SQLite integrity before use.

Restore into a new directory on the data filesystem, such as `/srv/delivery-recovery/data/restore/<run-id>`, preserving the original archive and prior active dataset. `restore-switch.sh switch` activates a verified candidate through the mount-checked path. Verify logical run/revision/audit contents, restart the service and verify again. Rollback must select the known prior dataset and verify its contents; a successful HTTP status alone is insufficient. Review `scripts/phase4_backup_restore.py --help` and the restore-window helper before constructing environment-specific commands.

## Pause, rollback and teardown

Stopping compute does not remove storage, images, snapshots or logs. Inventory the scoped resources and retained recovery assets, then prepare an exact deletion plan. Preserve local backup/image/log assets before deletion. Read back absence and remaining resources afterward, reporting denied inventory queries honestly. Removing a resource is irreversible even if source permits later recreation; verify recreation separately rather than assuming it.

No generic document authorizes cloud spending or changes. Establish environment-specific authority and operational limits before execution. Personal schedules, spending approvals and rendered receipts belong outside the public repository.

## Local report files

IAM bundle summaries are generated beside the rendered inputs in `var/phase4/iam`. Price provenance and estimates are generated in `var/phase4/cost-reports`. These directories are gitignored. Generated files must not be copied into public documentation without an independent disclosure and audience review.
