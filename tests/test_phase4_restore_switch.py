"""R3: restore-switch on a real disposable mounted filesystem."""

from __future__ import annotations

import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "tests" / "bootstrap" / "run_restore_switch_tests.sh"


def test_restore_switch_is_stable_and_rolls_back(posix_harness_command):
    assert HARNESS.is_file(), "restore-switch harness is missing"
    completed = subprocess.run(
        posix_harness_command(HARNESS, root=True),
        capture_output=True,
        text=True,
        timeout=180,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    for case in (
        "switch_stable_mount",
        "rollback_previous",
        "same_filesystem_required",
        "mountpoint_required",
        "start_failure_rollback",
        "traversal_rejected",
        "symlink_escape_rejected",
        "current_nested_rejected",
        "existing_release_rejected",
    ):
        assert f"PASS {case}" in output, output
