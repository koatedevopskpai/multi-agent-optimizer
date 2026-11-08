# Verification results

## Ubuntu / Python 3.12

[CI run on 8 November 2026](https://github.com/koatedevopskpai/multi-agent-optimizer/actions/runs/42446775012), commit `d5f4032`, Ubuntu 26.04.1 / CPython 3.12.16:

| Check | Observed result |
|---|---|
| Python tests | 564 passed, no test skips |
| Combined line/branch coverage | 93.92% |
| Required branch-only coverage | All six module thresholds passed |
| Golden evaluation | 11/11 passed |
| Frontend tests | 8 passed across 2 files |
| Production UI build | Passed |
| npm dependency audit | Zero reported vulnerabilities |

The initial Linux run identified a missing executable flag on a shell script. The corrected run above executed the POSIX and hardlink checks successfully. Local Windows results are separate observations and are not substituted for Linux verification. Later documentation/tooling changes require their own CI result; the linked run remains the dated baseline above.

## Reproduction

Follow the root README. The `dev` extra includes the pinned SDK used by mocked IAM tests. Cloud identities and real provider keys are not needed for CI. Terraform tests use its built-in provider; POSIX harnesses use mocked service/device commands and a disposable filesystem. The mount harness requires root or passwordless sudo on Linux. Unavailable platform capabilities are reported as explicit skips.

The six coverage gates apply to candidate policy, contracts, provider execution, graph transitions, HTTP provider and trace export. Combined coverage is not presented as a branch-only percentage.

Historical cloud recovery results are described in [quality and evidence](QUALITY_AND_EVIDENCE.md). No current clean container-vulnerability attestation, production load result or disaster-recovery claim follows from these test results.
