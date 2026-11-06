> Historical engineering reference, adapted for publication. Execution packets and approval states below belong to the original development sequence; they are not current authorization or a turnkey deployment guide. Start with [the current portfolio guide](/docs/portfolio/START_HERE.md). Personal paths and identifiers have been replaced; original byte-preserved records remain in the private audit archive.

# Phase 4 IAM action/resource/condition matrix

Status: proposal only. No IAM resource is created or attached in preparation.
The current `default` profile is an IAM user with `AdministratorAccess` through
a group; that is **not** evidence that the proposed policies work.

## Identities

| Identity | Purpose | Credential path |
|---|---|---|
| Local named-profile deployer | Reviewed local plan/apply using a profile that resolves to the scoped deploy role | Explicit stage file `aws_profile = "focused-deploy"`; `Use-AwsProject.ps1 -VerifyIdentity` for the documented profile path |
| GitHub OIDC deploy role | CI deployment without long-lived keys | Deploy-only stage file `aws_profile = ""`; short-lived role credentials from `token.actions.githubusercontent.com`; never bootstrap |
| Operator | SSM sessions, start/stop, log read | MFA named profile that resolves to `focused-delivery-recovery-operator`; no operator OIDC role is prepared |
| EC2 runtime role | SSM managed instance, ECR pull, CloudWatch logs | Instance profile `focused-delivery-recovery-runtime`; no application approval authority |
| One-time IAM bootstrap | Create the runtime boundary/role/profile and the deploy/operator roles with their trust policies | MFA-human-only assumed role; no OIDC bootstrap path |

Terraform enforces `expected_account_id` and `expected_stage_role_name` with a
`terraform_data` precondition. The provider uses an explicit stage file:
`focused-bootstrap` for bootstrap stages and `focused-deploy` (or the deploy-only
standard chain) for deploy stages. A missing/mismatched account or role fails
plan/apply before mutation; `scripts/phase4_stage_exec.py` checks the actual
assumed role and executes the approved command in the same sanitized child
environment.

The named-profile deployment uses `deploy-named-profile-trust-policy.json`
(no OIDC statement). Optional OIDC deployment remains a separate trust. Live
one MFA device is assigned (Codex verified 4 November); no authenticated session or proposed role/profile exists yet.

## Deploy role (`infra/iam/deploy-policy.json`)

| Lifecycle | Actions | Resource scope | Condition |
|---|---|---|---|
| Discover | EC2/ECR/log/IAM describe/get/list actions | `*` for read-only discovery | None |
| Launch instance | `ec2:RunInstances` | project `instance/*` | `aws:RequestTag/Project=focused-delivery-recovery` |
| Launch dependencies | `ec2:RunInstances` | reviewed `ami/*`, `subnet/*`, `security-group/*`, `volume/*`, `network-interface/*` | Region/account scoped; no creation-tag condition on existing resources |
| Create volume | `ec2:CreateVolume` | project `volume/*` | Request tag Project |
| Create SG | `ec2:CreateSecurityGroup` | reviewed `vpc/*`, `security-group/*` | Region/account scoped |
| Tag on create | `ec2:CreateTags` | instance/volume/SG/ENI ARNs | `ec2:CreateAction` is RunInstances/CreateVolume/CreateSecurityGroup |
| Tag owned resources | `ec2:CreateTags` | instance/volume/SG/ENI ARNs | `aws:ResourceTag/Project=focused-delivery-recovery` |
| Mutate/delete compute | attach/detach/delete volume, terminate instance, SG egress revoke/authorize, delete SG, delete tags | instance/volume/SG ARNs | `aws:ResourceTag/Project=focused-delivery-recovery` |
| ECR login | `ecr:GetAuthorizationToken` | `*` (AWS requirement) | None |
| ECR repo/push | create/delete repo, lifecycle, scanning, tag mutability, upload/put image, batch get/delete | exact repository `focused-delivery-recovery` | None |
| Logs | create/delete group, retention, tag | exact log group `/aws/delivery-recovery/app*` | None |
| Pass role | `iam:PassRole` | exact runtime role | `iam:PassedToService=ec2.amazonaws.com` |

The earlier single `RunInstances` statement was split into new-instance and
existing-dependency statements. `CreateTags` is no longer unrestricted: it is
limited to tag-on-create or resources already carrying the project tag, so a
deployer cannot tag an unrelated resource and then mutate it. PassRole is
exact-role constrained. Runtime IAM writes are **not** in the deploy role.

## Bootstrap (`infra/iam/bootstrap-policy.json`)

Privileged one-time bootstrap creates the runtime role/profile, the
`focused-delivery-recovery-runtime-boundary` managed policy, the SSM managed
attachment and the inline ECR-pull/logs policies. The deploy role cannot
create IAM resources or write inline policies. Terraform's targeted bootstrap
apply runs under this identity before the ordinary deploy role is used.

## Runtime boundary (`infra/iam/runtime-boundary-policy.json`)

The runtime role has an independently created permissions boundary allowing only
SSM runtime channels, ECR pull and application log writes. It denies IAM, EC2
mutation and operator actions even if an inline/attached policy were altered.

## Account enforcement

The AWS provider sets `allowed_account_ids = [var.expected_account_id]`, so the
targeted IAM/ECR applies and every other provider call fail on a wrong account.
A `terraform_data` precondition is retained as a second guard, but the provider
boundary is the unavoidable dependency.

## Operator (`infra/iam/operator-policy.json`)

| Lifecycle | Actions | Resource scope | Condition |
|---|---|---|---|
| Inspect | EC2 describe addresses/instances/status/SGs/snapshots/volumes | `*` read-only | None |
| Pause/resume | `ec2:StartInstances`, `ec2:StopInstances` | project instance ARN | `ec2:ResourceTag/Project` |
| SSM session start | `ssm:StartSession` | project instance ARN + `AWS-StartPortForwardingSession` document | instance: `ssm:ResourceTag/Project`; document: no tag condition |
| SSM session lifecycle | terminate/resume on `session/*` | `session/*` ARN | None |
| SSM session list | `ssm:DescribeSessions` | `*` read-only | None |
| Connection status | `ssm:GetConnectionStatus` | project instance ARN | `ssm:ResourceTag/Project` |
| SSM command send | `ssm:SendCommand` | project instance ARN + `AWS-RunShellScript` document | instance: `ssm:ResourceTag/Project`; document: no tag condition |
| Command readback | `ssm:GetCommandInvocation`, `ssm:CancelCommand` | `*` (AWS lists no resource-level support) | None |
| Command list/inventory | `ssm:ListCommands`, `ssm:DescribeInstanceInformation` | `*` read-only | None |
| Logs | describe streams, filter/get events | exact log group | None |

The operator does **not** receive ECR push, IAM write or application approval
authority. SSM access is transport, not application authentication.

## Runtime (Terraform inline logs + managed policies)

`infra/iam/runtime-policy.json` is an equivalent reference for the inline
authority; the deployed role is built by `infra/terraform/main.tf`.

| Permission | Scope |
|---|---|
| SSM managed-instance core | AWS-managed policy |
| ECR pull | Inline `BatchCheckLayerAvailability`, `BatchGetImage`, `GetDownloadUrlForLayer` on the exact project repository; `GetAuthorizationToken` on `*` |
| `logs:CreateLogStream`, `DescribeLogStreams`, `PutLogEvents` | exact project log group |

The instance role cannot create IAM resources, read application secrets or
approve runs.

## Backup transfer

No S3 resource or S3 permission is proposed. The backup is transferred over an
SSM port-forwarded loopback HTTP listener, then the temporary listener is
stopped. This uses only SSM operator permissions and avoids an extra bucket,
lifecycle policy and data-retention decision.

## Validation

`tests/test_phase4_iam_proposal.py` checks the proposal itself with positive and
negative authorization cases and an SSM action/resource support map based on the
AWS Service Authorization Reference: GetCommandInvocation/CancelCommand use
`Resource: "*"` because AWS lists no resource-level support; GetConnectionStatus
targets the instance; session lifecycle uses `session/*`. These are custom policy
checks, **not** AWS authorization simulation or approval. The existing
administrator identity is not used as proof.

## Remaining IAM approvals

1. Create the runtime permissions boundary, runtime role/profile and its approved
   attachments under the MFA bootstrap identity.
2. Create the deploy and operator roles with their reviewed trust and inline
   policies under the same bootstrap identity.
3. Create private stage files and named profiles: `focused-bootstrap` and
   `focused-deploy` (or a deploy-only standard/OIDC chain). The common
   `terraform.tfvars` must not set `aws_profile`.
4. Confirm the existing GitHub OIDC provider is acceptable for the CI trust.
5. Apply the IAM change only after the deployment packet is approved.
