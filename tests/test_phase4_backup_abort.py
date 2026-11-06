"""Phase 4 backup quiescence abort behaviour."""

from __future__ import annotations

import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "tests" / "bootstrap" / "run_backup_abort_tests.sh"


def test_backup_aborts_before_copy_when_stop_or_mount_fails(posix_harness_command):
    assert HARNESS.is_file(), "backup abort shell harness is missing"
    completed = subprocess.run(
        posix_harness_command(HARNESS),
        capture_output=True,
        text=True,
        timeout=180,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    for case in (
        "stop_failure",
        "mount_failure",
        "discovery_failure",
        "continued_running",
        "success",
    ):
        assert f"PASS {case}" in output, output
