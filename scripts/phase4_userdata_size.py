"""Measure and verify the compact EC2 user-data bootstrap payload."""

from __future__ import annotations

import base64
import io
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO / "infra" / "bootstrap"
TEMPLATE = REPO / "infra" / "terraform" / "templates" / "user-data.sh.tftpl"
LIMIT_BYTES = 16_384

REPRESENTATIVE = {
    "${aws_region}": "eu-west-2",
    "${image_uri}": "123456789012.dkr.ecr.eu-west-2.amazonaws.com/focused-delivery-recovery@sha256:" + "a" * 64,
    "${log_group_name}": "/aws/delivery-recovery/app",
    "${project_tag}": "focused-delivery-recovery",
    "${volume_id}": "vol-0123456789abcdef0",
}


def build_bootstrap_archive() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(BOOTSTRAP.iterdir()):
            if path.is_file():
                info = zipfile.ZipInfo(path.name, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())
    return buffer.getvalue()


def render_user_data() -> str:
    archive_b64 = base64.b64encode(build_bootstrap_archive()).decode("ascii")
    text = TEMPLATE.read_text(encoding="utf-8")
    for placeholder, value in REPRESENTATIVE.items():
        text = text.replace(placeholder, value)
    text = text.replace("${bootstrap_archive_b64}", archive_b64)
    return text


def main() -> int:
    text = render_user_data()
    size = len(text.encode("utf-8"))
    print(f"user_data_bytes={size} limit={LIMIT_BYTES} within_limit={size <= LIMIT_BYTES}")
    return 0 if size <= LIMIT_BYTES else 1


if __name__ == "__main__":
    raise SystemExit(main())
