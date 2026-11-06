"""Bounded SSM archive upload command construction."""

from __future__ import annotations

import base64
from pathlib import Path

import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from phase4_ssm_upload import build_upload_commands  # noqa: E402


def test_small_archive_is_one_base64_chunk():
    payload = b"recovery-data"
    commands = build_upload_commands(payload, "/srv/delivery-recovery/incoming/r.tgz")
    assert any("base64 -d >" in command for command in commands)
    assert any("sha256sum" in command for command in commands)
    assert any("chown 10001:10001" in command for command in commands)
    assert sum("base64 -d" in command for command in commands) == 1
    expected = base64.b64encode(payload).decode("ascii")
    assert any(expected in command for command in commands)


def test_large_archive_is_chunked_without_losing_bytes():
    payload = b"x" * 100_000
    commands = build_upload_commands(payload, "/tmp/r.tgz", chunk_size=100_000)
    chunks = [command for command in commands if "base64 -d" in command]
    assert len(chunks) == 2
    assert chunks[0].count(">>") == 0
    assert chunks[1].count(">>") == 1
