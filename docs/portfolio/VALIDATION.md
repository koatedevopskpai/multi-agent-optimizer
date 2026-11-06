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

Windows, CPython 3.14.5. Following packaging review, the final backend rerun used this checkout's own `.venv` after the documented `python -m pip install -e ".[dev,langgraph]"`, with no `PYTHONPATH` override and no excluded test modules. `pip check` passed and imports resolved to the portfolio source. Frontend dependencies were freshly installed with `npm ci` in the earlier unchanged-UI check. No fresh Python 3.12/Linux environment result is claimed; remote CI has not run for this unpublished branch.

The review found that the earlier shared backend environment already contained `boto3`, hiding a missing development dependency. The `dev` extra now explicitly includes `boto3==1.43.123`, matching the IAM tooling requirements. Bootstrap tests are collected and executed with mocks rather than skipped for a missing SDK. The corrected project-local rerun again passed 559 tests with the same five OS-dependent skips and 93.94% coverage; all 11 golden cases passed.

Four POSIX harness tests skipped because WSL is inaccessible in the execution sandbox. A fifth hardlink-preservation test skipped because Windows denied hardlink creation. The publication runner executes POSIX harnesses using native bash on Linux; its disposable-mount harness requires root/passwordless sudo. These skips are explicit limitations, not passes. Earlier attempts hit sandbox temporary-directory restrictions; the final run used a writable dedicated test directory.

Coverage is the suite's combined line/branch measure, not a branch-only percentage. Required module branch-only thresholds are checked separately by the existing CI gate. The original historical AWS results are not rerun or newly claimed by these local checks. No AWS mutation, paid model call, push or publication occurred.

## Reproduce

Use the README commands. Local Windows sandbox users may need a writable `--basetemp` outside the checkout. Record skipped tests honestly and run the Linux CI job before treating those OS-dependent checks as verified for the publication branch. CI also installs Terraform for the local built-in-provider lifecycle tests; no AWS credentials are required.

Raw local validation logs stay outside the publication package to avoid exposing personal paths. Content identity is recorded in `release-manifest.json`; image bytes are also pinned separately in `approved-images.json`.
