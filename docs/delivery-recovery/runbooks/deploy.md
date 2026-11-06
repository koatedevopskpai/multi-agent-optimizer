> Historical engineering reference, adapted for publication. Execution packets and approval states below belong to the original development sequence; they are not current authorization or a turnkey deployment guide. Start with [the current portfolio guide](../../portfolio/START_HERE.md). Personal paths and identifiers have been replaced; original byte-preserved records remain in the private audit archive.

# Deploy

Preconditions: reviewed packet, explicit provisioning approval, selected private
subnet, scoped bootstrap/deploy/operator identities, and a locally built
`linux/arm64` image. No command below was run against AWS during preparation.

Full identity/switching detail: `PHASE4_ACCESS_AND_SEQUENCE.md`.

## 1. Read-only discovery with default

```powershell
& '<USER_HOME>\Documents\Codex\2026-11-01\w\outputs\Focused_Agent_Demo_Control\Use-AwsProject.ps1' -VerifyIdentity
.\.venv-focused\Scripts\python.exe scripts\phase4_network_selection.py
.\.venv-focused\Scripts\python.exe scripts\phase4_aws_readiness.py
```

`default` remains read-only; do not use its AdministratorAccess for mutation.
Actual selected IDs stay in `var/phase4/network-selection.json`. Set
`PHASE4_EVIDENCE_RUN` to a new run ID before refreshing evidence; never reuse a
historical evidence directory. The network checker aborts before
selection/output on an account mismatch and reports configured prerequisites,
not instance-observed reachability.

## 2. Stage credential files and Terraform initialization

Use explicit stage files. Do not put `aws_profile` in the common
`terraform.tfvars`.

| Private file | Contents |
|---|---|
| `terraform.tfvars` | common account, VPC/subnet, image URI, retention |
| `terraform.bootstrap.tfvars` | `aws_profile = "focused-bootstrap"`, bootstrap role name |
| `terraform.deploy.tfvars` | `aws_profile = "focused-deploy"`, deploy role name |
| `terraform.deploy-oidc.tfvars` | deploy-only runner chain: `aws_profile = ""`, deploy role name |

Copy the `.example` files. The bootstrap stage is **MFA-human-only**; there is no
OIDC bootstrap path.

From a fresh checkout:

```powershell
terraform -chdir=infra/terraform init
terraform -chdir=infra/terraform validate
```

## 3. Build the pinned image

```powershell
$Source = git rev-parse HEAD
docker buildx build --platform linux/arm64 `
  --build-arg SOURCE_REVISION=$Source `
  -t delivery-recovery:phase4 .
if ($LASTEXITCODE -ne 0) { throw "image build failed; stopping" }
```

If local Docker cannot build arm64, stop and record the blocker; do not push an
amd64-only image for a t4g instance. The registry digest is read back after push
in section 5; a pre-push local `RepoDigests` value is not the registry digest.

## 4. Bootstrap identity and runtime IAM apply

$Receipt = '<USER_HOME>\Documents\Codex\2026-11-01\w\outputs\Focused_Agent_Demo_Control\PHASE4_INDEPENDENT_REVIEW_RECEIPT_2026-11-04.json'

The initial bootstrap-role creation and MFA interaction are in
`PHASE4_ACCESS_APPROVAL_PACKET.md`; do not create it here with default-profile
credentials. This section starts after the bootstrap role exists and
`focused-bootstrap` profile resolves to it.

Run the command through `phase4_stage_exec.py`. It sanitizes its child
environment, verifies the assumed role and account, then executes in that same
environment; it does not change the caller's shell.

```powershell
# Follow the in-memory exported-session procedure in PHASE4_ACCESS_AND_SEQUENCE.md
# section 4; then run the reviewed wrapper with the real receipt:
.\\.venv-focused\\Scripts\\python.exe scripts\\phase4_runtime_iam_exec.py plan `
  --receipt $Receipt `
  --var-file terraform.tfvars `
  --var-file terraform.bootstrap-session.tfvars
if ($LASTEXITCODE -ne 0) { throw "runtime IAM plan failed; stopping" }
```

Terraform's provider cannot consume the MFA profile directly; use the exported
cached bootstrap session only in the child process and clear it in `finally`.

The wrapper passes the verified boundary as a plan-time
`runtime_boundary_policy_json` value and deletes its transient var file after
planning. The saved plan is self-contained for apply. Never retry the historical
DR-P4S path-based binary, and do not apply until Codex accepts the corrected
inputs and the fresh plan.

The bootstrap policy owns runtime IAM creation and permissions-boundary
operations. The deploy policy cannot create IAM roles or write inline policies.
The inline logs policy uses a static ARN, so this IAM-targeted apply has no
CloudWatch resource dependency.

## 5. Deploy identity: ECR, tag, push, digest readback

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

Then tag/push and read the immutable digest back from ECR. Every AWS call is
wrapped; Docker commands are guarded by their own exit codes.

```powershell
$StageArgs = @(
  "scripts\phase4_stage_exec.py",
  "--stage", "deploy-image",
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

Put `$Immutable` in the private `terraform.tfvars` `image_uri`. The ECR readback
is authoritative. Do not invent a tagless push or compare against a pre-push
local `RepoDigests` value.

## 6. Saved plan and apply

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
# Review resources, tags, account/role guard, selected public subnet, image digest and cost.

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

The provider `allowed_account_ids` and Terraform account/role precondition must
both pass. User data installs the compressed bootstrap archive, resolves the
exact data EBS volume by volume ID/serial, mounts and verifies it, and starts the
systemd-guarded service.

## 7. Operator access and smoke

Run the SSM command through the operator stage runner:

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

On EC2:

```bash
/usr/local/lib/delivery-recovery/service-control.sh verify
systemctl status delivery-recovery
curl -fsS http://127.0.0.1:8000/readyz
```

Run the in-instance business smoke and correlate the run ID with CloudWatch.
SSM is transport, not application authentication.

## 8. Drills and teardown

Use the operator role for SSM/start-stop/logs, the deploy role for Terraform/
image actions, and the bootstrap role for IAM deletion. Run every destroy or
cleanup command through the matching stage runner and stop on a nonzero exit.
Follow the failure, backup/restore, pause/resume, rollback and teardown runbooks.
Backup transfer is SSM-only; no S3 resource is created. Stop expensive resources
during pauses.
