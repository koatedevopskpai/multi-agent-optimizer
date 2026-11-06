> Historical engineering reference, adapted for publication. Execution packets and approval states below belong to the original development sequence; they are not current authorization or a turnkey deployment guide. Start with [the current portfolio guide](../portfolio/START_HERE.md). Personal paths and identifiers have been replaced; original byte-preserved records remain in the private audit archive.

# Phase 4 access and deployment sequence

Status: prepared, **not executed**. Conditional IAM access approval is recorded;
Codex re-review of changed policy bytes/sequence is pending. No IAM change,
Terraform apply/import/destroy, ECR push or cloud drill has been performed. The
approved budget remains GBP20 total gross; stop threshold GBP12.

## Source and network selection

- Source: correction commits through the conditional-access preparation; this
  document is refreshed with the MFA, saved-plan and ECR-scope corrections.
- Read-only selection was performed with profile `default`, expected account and
  `eu-west-2`.
- Four default-VPC subnets were evaluated. The checker now respects NACL
  direction, IPv4/IPv6 family, CIDR scope, protocol and first-match rule order.
  Public egress/return requires `0.0.0.0/0` coverage, and the full TCP/UDP return
  interval 1024-65535 is proved rather than sampling one port.
- Ordinary DNS NACL rules are reported separately from the AmazonProvidedDNS
  exception at 169.254.169.253. Only IPv4 is evaluated.
- The deterministic first eligible subnet is in `eu-west-2a`. Its actual
  identifiers are in the gitignored private file
  `var/phase4/network-selection.json`; public evidence redacts them.
- This is configured prerequisite evidence, **not** reachability observed from
  an EC2 instance. SSM/ECR/CloudWatch reachability still requires the first
  instance boot check.

## Identities

| Identity | Role / credential path | Scope |
|---|---|---|
| Read-only discovery | Existing `default` profile / `Use-AwsProject.ps1 -VerifyIdentity` | Describe/read only; never used for mutation |
| Bootstrap | Assumed role `focused-delivery-recovery-bootstrap` from the privileged human principal with MFA | One-time runtime boundary/role/profile, deploy and operator role creation |
| Deployer (named profile) | `focused-delivery-recovery-deploy` via source profile + MFA | ECR, compute, EBS, logs, SG, PassRole; no IAM create/inline writes |
| Deployer (OIDC, optional) | GitHub OIDC `token.actions.githubusercontent.com`, exact repo/branch subject | Deploy stages only; never bootstrap; not required for this demonstration |
| Operator | `focused-delivery-recovery-operator` via MFA | SSM sessions/commands, start/stop, log read; no ECR/IAM write |
| Runtime | EC2 role `focused-delivery-recovery-runtime` + permissions boundary | SSM channels, ECR pull, logs only |

Bootstrap trust is **MFA human-only**. Do not widen it to OIDC for convenience.
The named-profile deploy path uses
`deploy-named-profile-trust-policy.json` and contains no OIDC statement. The
separate OIDC deploy trust is optional and not a prerequisite for this
demonstration. One MFA device is assigned to the human principal (Codex verified
4 November); no MFA-authenticated session exists yet.

Policy documents: `infra/iam/bootstrap-policy.json`,
`deploy-policy.json`, `operator-policy.json`, `runtime-boundary-policy.json`.
Trust documents: `bootstrap-trust-policy.json`,
`deploy-named-profile-trust-policy.json`, `operator-trust-policy.json`,
`runtime-trust-policy.json`, plus optional `deploy-trust-policy.json` /
`github-oidc-trust-policy.json`.

`PHASE4_IAM_MATRIX.md` maps every action/resource/condition. Policy tests are
custom semantics checks, not AWS authorization simulation.

## 0. Read-only discovery

Use the existing default profile only for checks:

```powershell
& '<USER_HOME>\Documents\Codex\2026-11-01\w\outputs\Focused_Agent_Demo_Control\Use-AwsProject.ps1' -VerifyIdentity
.\.venv-focused\Scripts\python.exe scripts\phase4_network_selection.py
.\.venv-focused\Scripts\python.exe scripts\phase4_aws_readiness.py
```

Do not run Terraform or AWS mutations with `default`. The network checker aborts
before selection or output when the expected account is not configured or does
not match. Set `PHASE4_EVIDENCE_RUN` to a new run ID before refreshing evidence;
never reuse a historical evidence directory.

## 1. Render private IAM documents

Identifiers remain under `var/phase4/iam` (gitignored):

```powershell
.\.venv-focused\Scripts\python.exe scripts\phase4_render_iam.py `
  --account-id <account-id> `
  --bootstrap-principal-arn <admin-or-bootstrap-principal-arn> `
  --deployer-principal-arn <deployer-principal-arn> `
  --operator-principal-arn <operator-principal-arn>
```

The renderer refuses unresolved placeholders. Pin the active named-profile
bundle and hashes, excluding optional OIDC documents:

```powershell
.\.venv-focused\Scripts\python.exe scripts\phase4_iam_manifest.py
```

This writes the private execution manifest and the public hash manifest
`docs/delivery-recovery/phase4-active-iam-manifest.json` (9 active documents, 2
optional OIDC documents excluded). Changed bytes require Codex re-review.

## 2. One-time IAM bootstrap (privileged, not executed)

$Receipt = '<USER_HOME>\Documents\Codex\2026-11-01\w\outputs\Focused_Agent_Demo_Control\PHASE4_INDEPENDENT_REVIEW_RECEIPT_2026-11-04.json'

Approval detail: `PHASE4_ACCESS_APPROVAL_PACKET.md`. One MFA device is assigned
to the human principal (Codex verified 4 November); the local MFA interaction is
not yet authenticated.

Codex writes the independent receipt at
`var/phase4/independent-review-receipt.json` after reviewing the actual active
manifest. Schema: `phase4-independent-review-receipt.schema.json`. It pins the
manifest digest, schema, active count and Terraform source hash. Execution tools
never create or overwrite it; missing/mismatched receipt stops before MFA/AWS.

The initial mutation uses an MFA-derived short-lived session, not direct
long-lived `default` credentials. GetSessionToken cannot take a session policy
and does not reduce the user's permissions; the session is privileged and limited
by this fixed executor to creating the named bootstrap role and attaching its
reviewed inline policy.

```powershell
.\.venv-focused\Scripts\python.exe scripts\phase4_initial_bootstrap.py --execute --receipt $Receipt
```

The script prompts privately with `getpass` (no echo), obtains the session in
memory, verifies account/principal/expiry, hash-checks
`bootstrap-trust-policy.json` and `bootstrap-policy.json` against
`var/phase4/iam/active-manifest.json`, refuses to overwrite an existing bootstrap
role, and prints status/expiry only. No code or credential enters chat, argv,
history or evidence.

After success, assume the bootstrap role and create the deploy/operator roles.
The bootstrap policy permits lifecycle operations only on these named roles; the
runtime boundary/role/profile are created later by the IAM-only saved Terraform
plan.

Configure the named profiles before the next stage. `focused-bootstrap` must
resolve to the assumed bootstrap role, and `focused-deploy` to the deploy role.
Every subsequent AWS CLI/Terraform command is executed through the stage runner,
which verifies the role/account and then runs the command in the same sanitized
child environment:

```powershell
.\.venv-focused\Scripts\python.exe scripts\phase4_stage_exec.py `
  --stage bootstrap-deploy-role --profile focused-bootstrap `
  --expected-account <account-id> `
  --expected-role focused-delivery-recovery-bootstrap `
  -- python scripts\phase4_iam_cli_exec.py create-deploy `
     --profile focused-bootstrap --expected-account <account-id> --receipt $Receipt
if ($LASTEXITCODE -ne 0) { throw "deploy role creation failed; stopping" }

.\.venv-focused\Scripts\python.exe scripts\phase4_stage_exec.py `
  --stage bootstrap-deploy-policy --profile focused-bootstrap `
  --expected-account <account-id> `
  --expected-role focused-delivery-recovery-bootstrap `
  -- python scripts\phase4_iam_cli_exec.py put-deploy-policy `
     --profile focused-bootstrap --expected-account <account-id> --receipt $Receipt
if ($LASTEXITCODE -ne 0) { throw "deploy policy attachment failed; stopping" }

.\.venv-focused\Scripts\python.exe scripts\phase4_stage_exec.py `
  --stage bootstrap-operator-role --profile focused-bootstrap `
  --expected-account <account-id> `
  --expected-role focused-delivery-recovery-bootstrap `
  -- python scripts\phase4_iam_cli_exec.py create-operator `
     --profile focused-bootstrap --expected-account <account-id> --receipt $Receipt
if ($LASTEXITCODE -ne 0) { throw "operator role creation failed; stopping" }

.\.venv-focused\Scripts\python.exe scripts\phase4_stage_exec.py `
  --stage bootstrap-operator-policy --profile focused-bootstrap `
  --expected-account <account-id> `
  --expected-role focused-delivery-recovery-bootstrap `
  -- python scripts\phase4_iam_cli_exec.py put-operator-policy `
     --profile focused-bootstrap --expected-account <account-id> --receipt $Receipt
if ($LASTEXITCODE -ne 0) { throw "operator policy attachment failed; stopping" }
```

The runner removes conflicting `AWS_ACCESS_KEY_ID`, secret, session-token and
profile variables **inside its child process only** for a named-profile stage,
sets only the reviewed profile, verifies the assumed role and account, then runs
the command with that same environment. It does not change the caller's
PowerShell environment and never logs credential values. A failed lookup,
wrong account/role or non-assumed-role principal prevents the command from being
invoked. Use `if ($LASTEXITCODE -ne 0) { throw ... }` after every wrapper call so
a native failure stops dependent commands.

The runtime trust document (`runtime-trust-policy.json`) is a reference for the
Terraform-generated EC2 trust. Boundary changes use
`iam:PutRolePermissionsBoundary`; teardown uses
`iam:DeleteRolePermissionsBoundary`, both in the bootstrap policy. The bootstrap
policy does **not** grant `iam:CreateUser`, access-key creation or policy
attachment outside the reviewed roles.

Ownership is explicit: bootstrap, deploy and operator are checked CLI stages;
runtime role/boundary/inline policies/SSM attachment/instance profile are the
IAM-only Terraform group. Do not invent Terraform addresses for the CLI roles or
import them into state.

## 3. Stage credential files and Terraform initialization

Terraform refuses to run mutation unless both the account and the expected
assumed-role name match. The provider profile comes from an explicit stage file,
not from a stale `aws_profile` in the common file.

Private, gitignored files:

| File | Required values |
|---|---|
| `infra/terraform/terraform.tfvars` | common account, VPC/subnet, image URI, retention; **no `aws_profile`** |
| `infra/terraform/terraform.bootstrap.tfvars` | `aws_profile = "focused-bootstrap"`, `expected_stage_role_name = "focused-delivery-recovery-bootstrap"` |
| `infra/terraform/terraform.deploy.tfvars` | `aws_profile = "focused-deploy"`, `expected_stage_role_name = "focused-delivery-recovery-deploy"` |
| `infra/terraform/terraform.deploy-oidc.tfvars` | only for a runner with deploy-role credentials: `aws_profile = ""`, same deploy role name |

Copy the matching `.example` files and fill the private values. From a fresh
checkout, **before any targeted apply**:

```powershell
terraform -chdir=infra/terraform init
terraform -chdir=infra/terraform validate
```

## 4. Runtime IAM saved plan and reviewed apply

The runtime IAM stage uses the fixed `phase4_runtime_iam_exec.py` wrapper. Terraform's
AWS provider cannot consume the MFA role profile directly (no AssumeRole token
provider), so the reviewed procedure exports the cached bootstrap session in
memory, removes `AWS_PROFILE` for the Terraform process, verifies the exported
identity, and uses a private `terraform.bootstrap-session.tfvars` with
`aws_profile = ""`. No credentials are written to disk or printed.

The wrapper verifies the exact rendered boundary bytes and passes them to
Terraform as the `runtime_boundary_policy_json` input value through a private,
transient `.tfvars.json` file. Terraform records that value in the saved plan, so
the file is deleted immediately after planning and apply does not depend on it.
A policy passed only as a file path is not sufficient: the DR-P4S apply failed
because `templatefile()` referenced the deleted temporary path. Never retry or
restore acceptance for that historical binary.

```powershell
$creds = (aws configure export-credentials --profile focused-bootstrap --format process | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0) { throw "bootstrap credential export failed; stopping" }
try {
  $env:AWS_ACCESS_KEY_ID = $creds.AccessKeyId
  $env:AWS_SECRET_ACCESS_KEY = $creds.SecretAccessKey
  $env:AWS_SESSION_TOKEN = $creds.SessionToken
  Remove-Item Env:AWS_PROFILE -ErrorAction SilentlyContinue
  $ident = (aws --region eu-west-2 --no-cli-pager sts get-caller-identity --output json | ConvertFrom-Json)
  if ($ident.Account -ne '<account-id>' -or $ident.Arn -notmatch ':assumed-role/focused-delivery-recovery-bootstrap/') {
    throw "exported bootstrap session identity mismatch; stopping"
  }
  .\.venv-focused\Scripts\python.exe scripts\phase4_runtime_iam_exec.py plan `
     --receipt $Receipt `
     --var-file terraform.tfvars `
     --var-file terraform.bootstrap-session.tfvars
  if ($LASTEXITCODE -ne 0) { throw "runtime IAM plan failed; stopping" }
} finally {
  Remove-Item Env:AWS_ACCESS_KEY_ID,Env:AWS_SECRET_ACCESS_KEY,Env:AWS_SESSION_TOKEN -ErrorAction SilentlyContinue
}
```

`terraform.bootstrap-session.tfvars` contains only `aws_profile = ""` and
`expected_stage_role_name = "focused-delivery-recovery-bootstrap"`; it is private
and never committed. Inspect `infra/terraform/phase4-runtime-iam.json` and confirm
only the approved runtime IAM addresses, no ECR/compute/EBS/log/network/destroy
actions, and the exact planned boundary value. The account guard may be a no-op
alongside six IAM creates; do not require seven creates mechanically. Stop for
Codex review of the new binary and raw JSON before apply.

After Codex accepts the corrected inputs and records the fresh plan hashes,
apply that exact binary using the same exported-session pattern and explicit
absolute `--plan` and `--receipt` paths:

```powershell
.\.venv-focused\Scripts\python.exe scripts\phase4_runtime_iam_exec.py apply `
  --plan '<USER_HOME>\development\multi-agent-optimizer\var\phase4\plan\phase4-runtime-iam.tfplan' `
  --receipt $Receipt
if ($LASTEXITCODE -ne 0) { throw "runtime IAM apply failed; stopping" }
```

The wrapper refuses a changed plan/receipt digest, a changed manifest/source, or
a failed derived-JSON scope check. It never reads the deleted transient policy
file during apply. Inline logs and ECR policy use static ARNs, so neither pulls a
deploy-owned resource into the IAM plan.

## 5. Switch to deploy identity; create ECR and image

Both Terraform and AWS CLI commands run through the deploy stage runner. The
explicit deploy stage file is still required for Terraform; the wrapper verifies
the actual identity and provides the sanitized child environment for both.

```powershell
.\.venv-focused\Scripts\python.exe scripts\phase4_stage_exec.py `
  --stage deploy-ecr --profile focused-deploy `
  --expected-account <account-id> `
  --expected-role focused-delivery-recovery-deploy `
  -- terraform -chdir=infra/terraform apply `
     -target=terraform_data.account_guard `
     -target=aws_ecr_repository.app `
     -target=aws_ecr_lifecycle_policy.app `
     -var-file=terraform.tfvars `
     -var-file=terraform.deploy.tfvars
if ($LASTEXITCODE -ne 0) { throw "deploy-ecr failed; stopping" }
```

Build, tag, push and read the immutable digest back from ECR:

```powershell
$Source = git rev-parse HEAD
docker buildx build --platform linux/arm64 `
  --build-arg SOURCE_REVISION=$Source `
  -t delivery-recovery:phase4 .
if ($LASTEXITCODE -ne 0) { throw "image build failed; stopping" }

$StageArgs = @(
  "scripts\phase4_stage_exec.py",
  "--stage", "deploy-ecr-login",
  "--profile", "focused-deploy",
  "--expected-account", "<account-id>",
  "--expected-role", "focused-delivery-recovery-deploy",
  "--quiet", "--"
)
$Account = & .\.venv-focused\Scripts\python.exe @StageArgs `
  aws sts get-caller-identity --query Account --output text
if ($LASTEXITCODE -ne 0) { throw "deploy identity lookup failed; stopping" }
$Registry = "$Account.dkr.ecr.eu-west-2.amazonaws.com"
$Tag = "phase4-prep"
$Password = & .\.venv-focused\Scripts\python.exe @StageArgs `
  aws ecr get-login-password --region eu-west-2
if ($LASTEXITCODE -ne 0) { throw "ECR login token failed; stopping" }
$Password | docker login --username AWS --password-stdin $Registry
if ($LASTEXITCODE -ne 0) { throw "docker login failed; stopping" }

docker tag delivery-recovery:phase4 "${Registry}/focused-delivery-recovery:${Tag}"
if ($LASTEXITCODE -ne 0) { throw "image tag failed; stopping" }
docker push "${Registry}/focused-delivery-recovery:${Tag}"
if ($LASTEXITCODE -ne 0) { throw "image push failed; stopping" }

$Digest = & .\.venv-focused\Scripts\python.exe @StageArgs `
  aws ecr describe-images --repository-name focused-delivery-recovery `
  --image-ids imageTag=$Tag --query 'imageDetails[0].imageDigest' --output text
if ($LASTEXITCODE -ne 0) { throw "ECR digest readback failed; stopping" }
$Immutable = "${Registry}/focused-delivery-recovery@${Digest}"
& .\.venv-focused\Scripts\python.exe @StageArgs `
  aws ecr describe-images --repository-name focused-delivery-recovery `
  --image-ids imageDigest=$Digest `
  --query 'imageDetails[0].imageDigest' --output text
if ($LASTEXITCODE -ne 0) { throw "ECR digest verification failed; stopping" }
```

Put `$Immutable` into the private `terraform.tfvars` `image_uri`. The ECR
digest readback is authoritative; do not compare against a pre-push local
`RepoDigests` value, which is normally empty.

## 6. Saved plan and apply

The full application plan must run with an in-memory exported deploy session and
a private `terraform.deploy-session.tfvars` containing `aws_profile = ""` and
`expected_stage_role_name = "focused-delivery-recovery-deploy"`. Terraform cannot
consume the MFA deploy profile directly.

**Current prerequisite gap:** the accepted deploy policy does not include
`ec2:DescribeVpcAttribute`, which the AWS provider needs for
`data.aws_vpc.selected`. A full plan therefore aborts before producing a valid
saved plan. A reviewed read-only policy addition (or an approved alternative plan
identity) is required before rerunning. Do not bypass this with the default
profile. Also pass `-var-file` arguments as separate tokens; unquoted
`-var-file=name` is split by PowerShell and rejected by Terraform.



```powershell
.\.venv-focused\Scripts\python.exe scripts\phase4_stage_exec.py `
  --stage deploy-plan --profile focused-deploy `
  --expected-account <account-id> `
  --expected-role focused-delivery-recovery-deploy `
  -- terraform -chdir=infra/terraform plan `
     -var-file=terraform.tfvars `
     -var-file=terraform.deploy.tfvars `
     -out=phase4.tfplan
if ($LASTEXITCODE -ne 0) { throw "deploy-plan failed; stopping" }

terraform -chdir=infra/terraform show -no-color phase4.tfplan
if ($LASTEXITCODE -ne 0) { throw "plan review failed; stopping" }
# review exact resources, tags, account/role guard, image digest and cost

.\.venv-focused\Scripts\python.exe scripts\phase4_stage_exec.py `
  --stage deploy-apply --profile focused-deploy `
  --expected-account <account-id> `
  --expected-role focused-delivery-recovery-deploy `
  -- terraform -chdir=infra/terraform apply phase4.tfplan
if ($LASTEXITCODE -ne 0) { throw "deploy-apply failed; stopping" }

.\.venv-focused\Scripts\python.exe scripts\phase4_stage_exec.py `
  --stage deploy-output --profile focused-deploy `
  --expected-account <account-id> `
  --expected-role focused-delivery-recovery-deploy `
  -- terraform -chdir=infra/terraform output -json
if ($LASTEXITCODE -ne 0) { throw "output read failed; stopping" }
```

Provider `allowed_account_ids` and the Terraform account/role precondition must
both pass. The user data resolves the exact data EBS volume, verifies its mount,
installs the compressed bootstrap archive and starts the systemd-guarded
service.

## 7. Operator access and smoke

Run the SSM command through the operator stage runner. It verifies the operator
role/account and then executes in the same sanitized child environment.

```powershell
.\.venv-focused\Scripts\python.exe scripts\phase4_stage_exec.py `
  --stage operator --profile focused-operator `
  --expected-account <account-id> `
  --expected-role focused-delivery-recovery-operator `
  -- aws ssm start-session --target <instance-id> `
     --document-name AWS-StartPortForwardingSession `
     --parameters '{"portNumber":["8000"],"localPortNumber":["8000"]}'
if ($LASTEXITCODE -ne 0) { throw "operator session failed; stopping" }
```

On EC2 through SSM:

```bash
/usr/local/lib/delivery-recovery/service-control.sh verify
systemctl status delivery-recovery
curl -fsS http://127.0.0.1:8000/readyz
```

Then run the in-instance business smoke and correlate the run ID with
CloudWatch. SSM is transport; it is not application authentication.

## 8. Drills, backup, pause, rollback

Use the operator role for SSM/start-stop/logs and the deploy role only for
Terraform/image actions. Follow:

- `runbooks/failure-recovery.md`
- `runbooks/backup-restore.md` (staged inside `data/restore/<run-id>/restored`)
- `runbooks/pause-resume.md`
- `runbooks/rollback.md`

Backup transfer is SSM-only; no S3 bucket. Stop compute during pauses. If the
optional snapshot drill is used, every snapshot command also runs through the
deploy stage runner:

```powershell
.\.venv-focused\Scripts\python.exe scripts\phase4_stage_exec.py `
  --stage deploy-snapshot --profile focused-deploy `
  --expected-account <account-id> `
  --expected-role focused-delivery-recovery-deploy `
  -- aws ec2 create-snapshot --volume-id <project-volume-id> `
     --description "phase4 drill" `
     --tag-specifications 'ResourceType=snapshot,Tags=[{Key=Project,Value=focused-delivery-recovery}]'
if ($LASTEXITCODE -ne 0) { throw "snapshot drill failed; stopping" }
```

## 9. Teardown and recreation identity switches

Run every destroy/cleanup command through the matching stage runner and stop on
a nonzero exit. The runner verifies role/account before invoking Terraform or the
AWS CLI; Terraform still uses the explicit stage file.

1. Operator role: stop service/instance, capture inventory.
2. Deploy role: targeted `terraform destroy` for compute, EBS, SG, ECR, logs and
   the account guard only. Do not run an untargeted destroy from this identity.
3. Bootstrap role: targeted `terraform destroy` for the runtime boundary, role,
   inline policy, SSM managed attachment and instance profile.
4. Bootstrap role: delete the deploy/operator roles only if this project created
   them.
5. Privileged human principal: delete the bootstrap role's inline policy and the
   bootstrap role itself, last.
6. Verify active/billable inventory and remove any snapshot/transfer artifact.
7. Recreate for the interview using the preserved local recovery asset and
   rebuilt image; repeat the same identity switches and preflights.

Use `terraform.deploy.tfvars` for step 2 and `terraform.bootstrap.tfvars` for
step 3. The stage runner clears stale temporary credentials for named-profile
stages; it never logs their values. Do not delete the shared GitHub OIDC provider
or unrelated resources.

## 10. Post-execution audit with existing CloudTrail history

Do not create a trail, bucket, Lake store or extra logging. Use existing
CloudTrail management-event history:

```powershell
aws --profile default --region us-east-1 cloudtrail lookup-events `
  --lookup-attributes AttributeKey=EventName,AttributeValue=CreateRole
```

Query IAM global-service events in `us-east-1`; account for delivery delay and
ordering differences. Compare action/resource scope with the expected inventory,
not a brittle exact sequence. Unexpected mutation/resource scope is a stop
condition. CloudTrail is after-the-fact audit, not a preventive boundary.

## Remaining decisions / prerequisites

- Conditional IAM approval is recorded for the four named roles, MFA assignment,
  public subnet and one-time bootstrap exception. Do not request duplicate
  approval for unchanged scope.
- Codex must independently re-review changed rendered policy bytes and the
  corrected MFA/saved-plan sequence.
- Koate enters the MFA code privately in the local terminal when execution is
  ready; no code is requested in chat or evidence.
- After IAM access is established, generate/review the IAM, ECR and application
  plans under `PHASE4_RESOURCE_PLAN_REVIEW.md`; each resource plan has its own
  approval gate.
- Interview date remains unscheduled.

No approval is needed for read-only checks or local preparation. No optional S3
resource is part of this plan.
