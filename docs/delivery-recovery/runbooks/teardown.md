> Historical engineering reference, adapted for publication. Execution packets and approval states below belong to the original development sequence; they are not current authorization or a turnkey deployment guide. Start with [the current portfolio guide](../../portfolio/START_HERE.md). Personal paths and identifiers have been replaced; original byte-preserved records remain in the private audit archive.

# Teardown

Status: recovery is proven (native EC2 restore/switch/restart/terminal rollback).
**The billable cleanup is complete.** The accepted three-delete plan removed the
ECR lifecycle policy, the ECR repository with its three images, and the security
group. Earlier the instance/root, data volume, attachment, log group and account
guard were removed. Only the six zero-cost runtime-IAM managed resources remain
in Terraform state, plus the access roles outside it. CP-08 is complete for the
billable scope; runtime-IAM cleanup is separate and unapproved.
See `PHASE4_TEARDOWN_EXECUTION_PACKET.md`.

The eight-address plan was partially applied earlier and then superseded by the
reviewed permission correction and the accepted three-delete plan. Do not retry,
rebuild or reuse that obsolete plan.

Do not run an untargeted destroy. The remaining Terraform scope is **6 IAM
destroys**; deploy/operator/bootstrap roles are separate zero-cost cleanup, with
bootstrap last. The ECR repository no longer exists; the local image archive,
Dockerfile, source, lock and fresh-restore asset are retained.

Inventory was captured read-only at 2026-11-04T23:12:20Z with the cached
focused-operator role. ECR and IAM live reads were denied to that role; the
focused-deploy MFA session was unavailable. Do not use default/Admin to bypass
this. Raw private inventory/plans are under
`var/phase4/teardown-inventory-20261104T231220Z/` and `var/phase4/plan/`.

Remove every project-owned recurring resource and prove no unrelated resource
was touched. Teardown only after the local backup is hash-verified and a
fresh-environment restore has succeeded.

## 1. Preserve first

- Export the evidence manifest/hashes.
- Download the tested archive to a protected local directory.
- Preserve **both** the raw original and any explicit normalized derivative, plus
  their SHA-256 values and the normalization receipt. The executed cloud backup
  is 15,226 bytes (`9189d4d5...e1d45f0c`); the derivative is 14,540 bytes
  (`8963e0cd...7119ebc`). Both contain demo bearer identities and stay private.
- Re-run the explicit strict compatibility check on a copy before relying on it:
  `phase4_backup_restore.py import` against the derivative and `restore` into a
  fresh empty directory.
- Record source/image digest, Terraform outputs, run IDs and UTC timestamps.
- Do not rely on CloudWatch logs, a snapshot or the EBS volume as the only copy.
- Do not commit or package either identity-bearing archive in repository
  evidence; record only hashes and sanitized verification results.

## 2. Inventory active/billable state

Run this inventory with the read-only `default` profile or the operator role.
Terminated instances can remain visible for a short period. Evaluate
active/billable states, not an empty `describe-instances` list:

```powershell
aws ec2 describe-instances --region eu-west-2 `
  --filters "Name=tag:Project,Values=focused-delivery-recovery" `
  --query 'Reservations[].Instances[].[InstanceId,State.Name]' --output table
aws ec2 describe-volumes --region eu-west-2 `
  --filters "Name=tag:Project,Values=focused-delivery-recovery" `
  --query 'Volumes[].[VolumeId,Size,State]' --output table
aws ec2 describe-snapshots --owner-ids self --region eu-west-2 `
  --filters "Name=tag:Project,Values=focused-delivery-recovery" `
  --query 'Snapshots[].[SnapshotId,VolumeSize,State]' --output table
aws ecr describe-repositories --region eu-west-2 `
  --repository-names focused-delivery-recovery
aws logs describe-log-groups --region eu-west-2 `
  --log-group-name-prefix /aws/delivery-recovery
aws ec2 describe-addresses --region eu-west-2
aws ssm describe-instance-information --region eu-west-2
```

Record active counts: `pending/running/stopping/stopped` instance states,
`in-use/available` volumes, `pending/completed` snapshots, existing ECR/log
groups, Elastic IPs and SSM managed instances.

## 2a. Reviewed teardown-plan preparation

Do not delete anything until this preparation is reviewed and separately
approved. Teardown is not a drill and must not be bundled with a recovery window.

1. Freeze the inventory above with the exact UTC timestamp and command identity.
2. Map each project resource to its Terraform address or outside-Terraform owner
   (deploy role, operator role, bootstrap role, runtime IAM, ECR, logs, instance,
   root/data volumes, snapshots, any Elastic IP/SG). Mark shared/account-level
   resources that must not be touched, including the GitHub OIDC provider.
3. Confirm the local retention set in section 1 is hash-verified and a fresh
   import/restore from the retained derivative succeeds.
4. Record the proposed targeted destroy/addressing, expected action count,
   rollback route (recreate from Terraform/image recipe) and cost that stops.
5. Obtain Codex review of the concrete plan and Koate's separate resource
   approval. Then execute only the approved targets in sections 3-5 and record
   the after-inventory in section 6.
6. If the inventory differs from the reviewed list, stop and re-review; do not
   improvise a destroy.

## 3. Stop and remove the service

Use the operator role for SSM. EC2 through SSM:

```bash
/usr/local/lib/delivery-recovery/service-control.sh stop
/usr/local/lib/delivery-recovery/service-control.sh remove
systemctl disable delivery-recovery.service
```

Compose mode uses `--env-file /etc/delivery-recovery/service.env`; the helper
does this. Run mode uses `docker rm -f delivery-recovery`. Both preserve the
data volume.

## 4. Terraform destroy

Use the scoped deploy role. Do not use `default` AdministratorAccess. Execute the
destroy through the deploy stage runner so verification and the command share the
same sanitized child environment.

```powershell
.\.venv-focused\Scripts\python.exe scripts\phase4_stage_exec.py `
  --stage teardown-deploy --profile focused-deploy `
  --expected-account <account-id> `
  --expected-role focused-delivery-recovery-deploy `
  -- terraform -chdir=infra/terraform destroy `
     -target=aws_instance.app `
     -target=aws_volume_attachment.data `
     -target=aws_ebs_volume.data `
     -target=aws_security_group.app `
     -target=aws_ecr_lifecycle_policy.app `
     -target=aws_ecr_repository.app `
     -target=aws_cloudwatch_log_group.app `
     -target=terraform_data.account_guard `
     -var-file=terraform.tfvars `
     -var-file=terraform.deploy.tfvars
if ($LASTEXITCODE -ne 0) { throw "teardown-deploy failed; stopping" }
```

The ECR repository has `force_delete = true`; project images are removed with
it. The encrypted data volume has no `prevent_destroy` guard and is removed by
the reviewed targeted destroy. Do not run an untargeted destroy from the deploy
role: the runtime IAM objects are also in Terraform state but require the
bootstrap identity.

## 5. Remove outside-Terraform resources

$Receipt = '<USER_HOME>\Documents\Codex\2026-11-01\w\outputs\Focused_Agent_Demo_Control\PHASE4_INDEPENDENT_REVIEW_RECEIPT_2026-11-04.json'

Use the MFA bootstrap identity for IAM deletion. Execute the IAM destroy and
deletion commands through the bootstrap stage runner:

```powershell
.\.venv-focused\Scripts\python.exe scripts\phase4_stage_exec.py `
  --stage teardown-bootstrap --profile focused-bootstrap `
  --expected-account <account-id> `
  --expected-role focused-delivery-recovery-bootstrap `
  -- terraform -chdir=infra/terraform destroy `
     -target=aws_iam_instance_profile.app `
     -target=aws_iam_role_policy_attachment.ssm `
     -target=aws_iam_role_policy.ecr_pull `
     -target=aws_iam_role_policy.logs `
     -target=aws_iam_role.instance `
     -target=aws_iam_policy.runtime_boundary `
     -var-file=terraform.tfvars `
     -var-file=terraform.bootstrap.tfvars
if ($LASTEXITCODE -ne 0) { throw "teardown-bootstrap failed; stopping" }
```

Delete the deploy/operator roles with the fixed CLI executor, for example:

```powershell
.\.venv-focused\Scripts\python.exe scripts\phase4_stage_exec.py `
  --stage teardown-deploy-policy --profile focused-bootstrap `
  --expected-account <account-id> `
  --expected-role focused-delivery-recovery-bootstrap `
  -- python scripts\phase4_iam_cli_exec.py delete-deploy-policy `
     --profile focused-bootstrap --expected-account <account-id> --receipt $Receipt
if ($LASTEXITCODE -ne 0) { throw "delete deploy policy failed; stopping" }
```

Repeat for `delete-operator-policy`, `delete-deploy-role` and
`delete-operator-role`, then delete the bootstrap role last with the privileged
human principal. Do not run the bootstrap
role deletion through its own wrapper: a role cannot verify and delete itself
after its credentials are removed.

Project-owned cleanup includes:

- snapshot drill snapshot ID, deleted through the deploy stage runner;
- temporary transfer artifacts/listener under `/run`;
- deploy and operator roles created by the IAM bootstrap;
- the bootstrap role itself and its inline policy, deleted last by the
  privileged human principal;
- any temporary SG or Elastic IP (none proposed).

Never delete the shared account-level GitHub OIDC provider.

## 6. Verify cleanup

Run the inventory with `--profile default` (read-only) or the operator role, not
the deploy or bootstrap stage profile. Record the exact timestamps.

```powershell
aws --profile default ec2 describe-instances --region eu-west-2 `
  --filters "Name=tag:Project,Values=focused-delivery-recovery" `
  "Name=instance-state-name,Values=pending,running,stopping,stopped" `
  --query 'Reservations[].Instances[].InstanceId' --output json
aws ec2 describe-volumes --region eu-west-2 `
  --filters "Name=tag:Project,Values=focused-delivery-recovery" `
  --query 'Volumes[].VolumeId' --output json
aws ec2 describe-snapshots --owner-ids self --region eu-west-2 `
  --filters "Name=tag:Project,Values=focused-delivery-recovery" `
  --query 'Snapshots[].SnapshotId' --output json
aws ecr describe-repositories --region eu-west-2 `
  --repository-names focused-delivery-recovery
aws logs describe-log-groups --region eu-west-2 `
  --log-group-name-prefix /aws/delivery-recovery
aws ec2 describe-addresses --region eu-west-2 --query 'Addresses[].PublicIp'
```

Expected: no active/billable project resources, no repository/log group, no
project Elastic IP. Terminated instances may remain visible briefly; record
that as transient, not as an active resource. ECR/log Not Found errors are
acceptable evidence when recorded with the exact command/time.

## 7. Close the record

- Record final inventory and UTC teardown time.
- Note billing lag; no immediate zero-bill claim.
- Keep local evidence, Terraform, lockfile, image rebuild recipe, manifest hashes
  and the protected recovery asset (original + normalized derivative + receipt).
- Retain no cloud backup by default. A later retention decision needs duration,
  owner and cost.

## CP-08 facts

- Before/after active/billable inventory.
- Terraform destroy completion.
- No project instance/volume/snapshot/ECR/log/Elastic IP.
- Local backup hash verification and successful fresh restore from the retained
  strict-compatible derivative.
- Explicit statement that unrelated resources were not changed and terminated
  records are transient.
