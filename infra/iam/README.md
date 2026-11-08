# Reference IAM roles

These templates separate infrastructure control, service operations and application runtime. Replace placeholders for the intended environment before assessing policies. They are examples, not live account configuration.

| Role | Intended responsibility | Boundary |
|---|---|---|
| Bootstrap | Create scoped runtime and control-role resources | Separate from ordinary deployment; MFA-human trust in the named-profile example |
| Deploy | ECR, tagged compute/storage/security-group lifecycle and scoped PassRole | No runtime role creation or inline-policy writes |
| Operator | SSM, instance start/stop and log inspection | No IAM or ECR writes |
| Runtime | SSM channels, repository pull and application logging | EC2 service trust plus a permissions boundary |

SSM is transport and administration, not application authentication. Application planner/manager authorization is enforced separately by the server.

`deploy-policy.json` isolates selected VPC attribute reads, project-tagged mutations, exact runtime PassRole and London-only network-interface discovery. Discovery APIs may require Resource `*`; that does not grant mutation authority. The tests check allowed and denied contexts against the reference policy contracts; they do not replace AWS authorization simulation or live access verification.

Optional OIDC templates contain an illustrative repository/ref subject. Replace it with the exact intended deployment subject and assess the resulting trust before use. This repository's CI tests code; it does not deploy to AWS. No existing role assumption should be inferred from a template.

Render inputs and generated review reports belong under gitignored `var/phase4`. Never commit credentials, real-account renderings, state or execution receipts. See [reference operations](../../docs/operations/REFERENCE_OPERATIONS.md) for the generic lifecycle and guard requirements.
