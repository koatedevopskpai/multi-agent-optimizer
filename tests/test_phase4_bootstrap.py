"""Phase 4 bootstrap disk/mount tests.

The shell harness executes `infra/bootstrap/data-volume.sh` under WSL with mock
block-device, format, mount and findmnt commands. No real disk is touched.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "tests" / "bootstrap" / "run_bootstrap_tests.sh"


def test_bootstrap_disk_resolution_and_mount_fail_closed(posix_harness_command):
    assert HARNESS.is_file(), "bootstrap shell harness is missing"
    completed = subprocess.run(
        posix_harness_command(HARNESS),
        capture_output=True,
        text=True,
        timeout=180,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    for case in (
        "reversed_nvme_partitioned_root",
        "delayed_attachment",
        "missing_disk",
        "ambiguous_disk",
        "existing_ext4",
        "no_filesystem",
        "existing_non_ext4",
        "failed_mount",
        "expected_serial_on_root",
        "renamed_nvme_after_restart",
        "verify_mismatch",
        "verify_missing_mount",
    ):
        assert f"PASS {case}" in output, output
