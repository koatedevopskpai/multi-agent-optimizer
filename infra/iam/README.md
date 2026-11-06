# Phase 4 IAM proposals

## Optional OIDC branch binding

The OIDC subjects in the trust templates and Terraform defaults name the original
private audit branch as a historical reference. Before rendering an OIDC deployment
policy, replace the repository and branch subject with the exact intended deployment
ref and review it independently. Do not assume this portfolio branch can assume a
cloud role: this publication's CI only tests the code and does not deploy to AWS.
The named-profile reference path does not require GitHub OIDC.

## Scope

These files are review material only. They are not applied, imported or attached
by the preparation work.

- `runtime-policy.json` is a reference for the smallest extra inline logs policy
  granted to the instance role. Terraform generates the equivalent inline logs
  policy and repository-scoped ECR pull in `infra/terraform/main.tf`; the EC2 role
  also uses the AWS-managed `AmazonSSMManagedInstanceCore` policy. The
  account-wide ECR managed policy is no longer attached. SSM is
  transport/administration, not application authentication.
- `deploy-policy.json` is the scoped ordinary deploy identity. Replace
  `<account-id>` after review. It includes ECR login/push, SG egress, EC2
  lifecycle, project-tagged snapshot drill, IAM read, restricted CreateTags and
  exact-role PassRole. It cannot create IAM roles/profiles or write runtime
  inline policies.
- `bootstrap-policy.json` is the separate privileged IAM bootstrap for the
  runtime role/profile, runtime permissions boundary, approved attachments and
  the fixed-name deploy/operator roles.
- `runtime-boundary-policy.json` is the runtime permissions boundary; it allows
  only SSM channels, ECR pull and application logs.
- `operator-policy.json` separates SSM sessions/commands, start/stop and log
  read from deployment. It has no IAM/ECR write authority.
- `bootstrap-trust-policy.json` and `operator-trust-policy.json` require an
  MFA-authenticated human principal; there is no OIDC bootstrap path. The live
  one MFA device is assigned to the human user; no authenticated session exists yet.
  `runtime-trust-policy.json` is the EC2 service trust.
- `deploy-named-profile-trust-policy.json` is the named-profile deploy trust used
  for this demonstration; it contains no OIDC statement.
- `github-oidc-trust-policy.json` is the optional deploy-only OIDC reference. It
  does not apply to the bootstrap role. `deploy-trust-policy.json` combines the
  named-profile deployer and optional OIDC subject.
- `../docs/delivery-recovery/PHASE4_IAM_MATRIX.md` maps each lifecycle action,
  resource and condition to the relevant identity.
- `../docs/delivery-recovery/PHASE4_ACCESS_AND_SEQUENCE.md` contains the exact
  one-time bootstrap commands, credential switches, deployment order and
  teardown identity switches.

Do not add account IDs, ARNs, credentials or policy review notes containing
secrets to public evidence. The deployment packet records redacted findings.
