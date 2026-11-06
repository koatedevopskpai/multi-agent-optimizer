"""Upload a recovery archive to the EC2 instance over SSM without S3.

The archive is base64-encoded into one or more SSM RunShellScript chunks. This
is intentionally bounded to small synthetic recovery assets; large archives
should use a separately reviewed transfer mechanism.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import tempfile
from pathlib import Path

CHUNK_SIZE = 60_000


def build_upload_commands(
    payload: bytes, remote_path: str, chunk_size: int = CHUNK_SIZE
) -> list[str]:
    encoded = base64.b64encode(payload).decode("ascii")
    chunks = [encoded[i : i + chunk_size] for i in range(0, len(encoded), chunk_size)]
    if not chunks:
        chunks = [""]
    commands = [
        f"install -d -m 0700 -o 10001 -g 10001 \"$(dirname {remote_path})\"",
    ]
    for index, chunk in enumerate(chunks):
        operator = ">" if index == 0 else ">>"
        commands.append(f"printf '%s' '{chunk}' | base64 -d {operator} {remote_path}")
    commands.append(f"chown 10001:10001 {remote_path}")
    commands.append(f"sha256sum {remote_path}")
    return commands


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--remote-path", required=True)
    parser.add_argument("--profile", default="default")
    parser.add_argument("--region", default="eu-west-2")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    archive = Path(args.archive)
    if not archive.is_file():
        raise SystemExit("archive does not exist")
    if not args.remote_path.startswith("/"):
        raise SystemExit("remote path must be absolute")
    commands = build_upload_commands(archive.read_bytes(), args.remote_path)
    if args.dry_run:
        print(json.dumps({"commands": len(commands), "bytes": archive.stat().st_size}))
        return 0

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump({"commands": commands}, handle)
        params_path = handle.name
    try:
        command = [
            "aws",
            "ssm",
            "send-command",
            "--instance-ids",
            args.instance_id,
            "--document-name",
            "AWS-RunShellScript",
            "--parameters",
            f"file://{params_path}",
            "--profile",
            args.profile,
            "--region",
            args.region,
            "--query",
            "Command.CommandId",
            "--output",
            "text",
        ]
        sent = subprocess.run(command, capture_output=True, text=True, timeout=60)
        if sent.returncode != 0:
            raise SystemExit(sent.stderr.strip())
        command_id = sent.stdout.strip()
        print(json.dumps({"command_id": command_id, "commands": len(commands)}))
        return 0
    finally:
        Path(params_path).unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
