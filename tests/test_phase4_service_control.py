"""Phase 4 service-control behaviour for Compose and docker-run modes."""

from __future__ import annotations

import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "tests" / "bootstrap" / "run_service_control_tests.sh"


def test_service_control_stop_remove_start_and_rollback(posix_harness_command):
    assert HARNESS.is_file(), "service-control shell harness is missing"
    completed = subprocess.run(
        posix_harness_command(HARNESS),
        capture_output=True,
        text=True,
        timeout=180,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    for case in (
        "compose_start",
        "compose_stop",
        "compose_replace",
        "run_start_absent",
        "run_start_existing",
        "run_rollback",
        "failed_stop",
        "verify_failure",
        "already_stopped",
        "inspect_error",
        "compose_ps_error",
        "continued_running",
    ):
        assert f"PASS {case}" in output, output
