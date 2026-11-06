"""Actual subprocess restart drill for pending approval and commit window."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "phase3_restart_drill.py"


def _run(phase: str, mode: str, data: Path) -> dict:
    started = time.monotonic()
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            phase,
            "--mode",
            mode,
            "--data",
            str(data),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=90,
    )
    elapsed = time.monotonic() - started
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    payload["process_seconds"] = round(elapsed, 3)
    return payload


def test_pending_approval_survives_actual_process_restart(tmp_path):
    data = tmp_path / "approval"
    prepared = _run("prepare", "approval", data)
    assert prepared["status_at_exit"] == "awaiting_approval"
    recovered = _run("recover", "approval", data)
    assert recovered["status_after_restart"] == "completed"
    assert recovered["revision"] == 1
    assert recovered["revisions"][0]["revision"] == 1
    assert recovered["process_seconds"] <= 60


def test_commit_before_checkpoint_survives_actual_process_restart(tmp_path):
    data = tmp_path / "crash-window"
    prepared = _run("prepare", "crash-window", data)
    assert prepared["status_at_exit"] == "approved"
    recovered = _run("recover", "crash-window", data)
    assert recovered["status_after_restart"] == "completed"
    assert recovered["revision"] == 1
    assert len(recovered["revisions"]) == 1
    assert recovered["audit_events"].count("DECISION_APPROVED") == 1
    assert recovered["process_seconds"] <= 60
