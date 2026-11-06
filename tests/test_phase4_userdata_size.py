"""R5: compact bootstrap distribution and rendered user-data size gate."""

from __future__ import annotations

import base64
import io
import zipfile
from pathlib import Path

import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import phase4_userdata_size as userdata  # noqa: E402


def test_rendered_user_data_is_under_the_ec2_limit():
    text = userdata.render_user_data()
    assert len(text.encode("utf-8")) <= userdata.LIMIT_BYTES
    assert "bootstrap_archive_b64" not in text
    assert "delivery-recovery-bootstrap.zip" in text


def test_bootstrap_archive_decodes_and_contains_all_scripts():
    archive = userdata.build_bootstrap_archive()
    with zipfile.ZipFile(io.BytesIO(archive)) as handle:
        names = set(handle.namelist())
    for name in (
        "data-volume.sh",
        "service-control.sh",
        "backup-export.sh",
        "restore-switch.sh",
        "delivery-recovery.service",
    ):
        assert name in names
    encoded = base64.b64encode(archive)
    decoded = base64.b64decode(encoded)
    assert decoded == archive
    with zipfile.ZipFile(io.BytesIO(decoded)) as handle:
        assert handle.read("data-volume.sh") == (userdata.BOOTSTRAP / "data-volume.sh").read_bytes()
