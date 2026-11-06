# Publication validation — 6 November 2026

These results belong to the curated publication checkout, separately from the original audit evidence.

| Check | Observed result |
|---|---|
| Python suite | **559 passed, 5 skipped**, 14 warnings; coverage **93.94%** |
| Required branch-only gates | All six module thresholds passed (at least 90%) |
| Golden evaluation | **11/11 passed** |
| Frontend interaction tests | **8 passed**, 2 files |
| Production UI build | Passed; 18 modules transformed |
| Application smoke | `/readyz`, `/ui/`, `/openapi.json` each returned HTTP200 via TestClient against the built UI |
| npm audit | Zero reported vulnerabilities at all severities |
| Trivy filesystem secret scan | Zero detected secrets; package/image vulnerability scanning is a different check |
| Image identity | All 12 selected derivatives match their approved SHA256 values |
| Audit archive | Complete Git bundle verified; original branch tip preserved |

## Environment and limits

Windows, CPython 3.14.5 and existing installed backend dependencies; `PYTHONPATH` explicitly selected this checkout's `src`. Frontend dependencies were freshly installed with `npm ci`. The backend was not independently installed into a fresh Python 3.12 environment here. CI is configured for Python 3.12/Ubuntu and has not run for this unpublished branch.

Four POSIX harness tests skipped because WSL is inaccessible in the execution sandbox. A fifth hardlink-preservation test skipped because Windows denied hardlink creation. The publication runner executes POSIX harnesses using native bash on Linux; its disposable-mount harness requires root/passwordless sudo. These skips are explicit limitations, not passes. Earlier attempts hit sandbox temporary-directory restrictions; the final run used a writable dedicated test directory.

Coverage is the suite's combined line/branch measure, not a branch-only percentage. Required module branch-only thresholds are checked separately by the existing CI gate. The original historical AWS results are not rerun or newly claimed by these local checks. No AWS mutation, paid model call, push or publication occurred.

## Reproduce

Use the README commands. Local Windows sandbox users may need a writable `--basetemp` outside the checkout. Record skipped tests honestly and run the Linux CI job before treating those OS-dependent checks as verified for the publication branch. CI also installs Terraform for the local built-in-provider lifecycle tests; no AWS credentials are required.

Raw local validation logs stay outside the publication package to avoid exposing personal paths. Content identity is recorded in `release-manifest.json`; image bytes are also pinned separately in `approved-images.json`.
