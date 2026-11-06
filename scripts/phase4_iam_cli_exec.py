"""Fixed-action CLI executor for deploy/operator IAM writes.

Loads the independently reviewed manifest and exact document bytes before any
AWS call, writes those bytes to a private temp file, verifies the caller is the
bootstrap role, and invokes only the approved AWS CLI operation. No arbitrary IAM
command or role name is accepted.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from phase4_iam_manifest_schema import verified_documents
from phase4_review_receipt import verify_accepted_inputs

REPO = Path(__file__).resolve().parents[1]
ENV_FILE = Path.home() / ".config" / "cloud-projects" / "focused-delivery-recovery.env"
RENDERED_DIR = REPO / "var" / "phase4" / "iam"
MANIFEST_FILE = RENDERED_DIR / "active-manifest.json"
RECEIPT_FILE = REPO / "var" / "phase4" / "independent-review-receipt.json"
BOOTSTRAP_ROLE = "focused-delivery-recovery-bootstrap"
ASSUMED_ROLE = re.compile(r":assumed-role/([^/]+)/")

ACTIONS = {
    "create-deploy": {
        "kind": "create-role",
        "role": "focused-delivery-recovery-deploy",
        "document": "deploy-named-profile-trust-policy.json",
    },
    "put-deploy-policy": {
        "kind": "put-role-policy",
        "role": "focused-delivery-recovery-deploy",
        "policy_name": "focused-deploy",
        "document": "deploy-policy.json",
    },
    "create-operator": {
        "kind": "create-role",
        "role": "focused-delivery-recovery-operator",
        "document": "operator-trust-policy.json",
    },
    "put-operator-policy": {
        "kind": "put-role-policy",
        "role": "focused-delivery-recovery-operator",
        "policy_name": "focused-operator",
        "document": "operator-policy.json",
    },
    "delete-deploy-policy": {
        "kind": "delete-role-policy",
        "role": "focused-delivery-recovery-deploy",
        "policy_name": "focused-deploy",
    },
    "delete-deploy-role": {
        "kind": "delete-role",
        "role": "focused-delivery-recovery-deploy",
    },
    "delete-operator-policy": {
        "kind": "delete-role-policy",
        "role": "focused-delivery-recovery-operator",
        "policy_name": "focused-operator",
    },
    "delete-operator-role": {
        "kind": "delete-role",
        "role": "focused-delivery-recovery-operator",
    },
}


def _expected_account() -> str | None:
    if not ENV_FILE.is_file():
        return None
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("AWS_EXPECTED_ACCOUNT_ID="):
            return line.split("=", 1)[1].strip()
    return None


def _identity(profile: str, runner, env: dict[str, str] | None) -> dict:
    command = [
        "aws",
        "--profile",
        profile,
        "--region",
        "eu-west-2",
        "--no-cli-pager",
        "sts",
        "get-caller-identity",
        "--output",
        "json",
    ]
    completed = runner(command, capture_output=True, text=True, env=env)
    if getattr(completed, "returncode", 1) != 0:
        raise RuntimeError("identity lookup failed")
    return json.loads(getattr(completed, "stdout", "") or "{}")


def _assert_identity(identity: dict, expected_account: str) -> None:
    arn = str(identity.get("Arn", ""))
    match = ASSUMED_ROLE.search(arn)
    role = match.group(1) if match else ""
    if identity.get("Account") != expected_account or role != BOOTSTRAP_ROLE:
        raise RuntimeError("caller is not the expected bootstrap role/account")


def _secure_temp(data: bytes) -> Path:
    fd, name = tempfile.mkstemp(prefix="phase4-iam-", suffix=".json")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        try:
            os.chmod(name, 0o600)
        except OSError:
            pass
        return Path(name)
    except Exception:
        os.close(fd)
        raise


def run_action(
    *,
    action: str,
    manifest_path: Path,
    rendered_dir: Path,
    receipt_path: Path,
    profile: str,
    expected_account: str,
    runner=subprocess.run,
    env: dict[str, str] | None = None,
) -> int:
    if action not in ACTIONS:
        raise RuntimeError("unapproved IAM action")
    spec = ACTIONS[action]
    _, manifest = verify_accepted_inputs(
        receipt_path=receipt_path,
        manifest_path=manifest_path,
        rendered_dir=rendered_dir,
    )
    document_bytes: bytes | None = None
    if "document" in spec:
        document_bytes = verified_documents(
            manifest, rendered_dir, (spec["document"],)
        )[spec["document"]]
    _assert_identity(_identity(profile, runner, env), expected_account)

    temp_path: Path | None = None
    try:
        if spec["kind"] == "create-role":
            temp_path = _secure_temp(document_bytes or b"")
            command = [
                "aws", "--profile", profile, "--region", "eu-west-2", "--no-cli-pager",
                "iam", "create-role", "--role-name", spec["role"],
                "--assume-role-policy-document", f"file://{temp_path}",
            ]
        elif spec["kind"] == "put-role-policy":
            temp_path = _secure_temp(document_bytes or b"")
            command = [
                "aws", "--profile", profile, "--region", "eu-west-2", "--no-cli-pager",
                "iam", "put-role-policy", "--role-name", spec["role"],
                "--policy-name", spec["policy_name"],
                "--policy-document", f"file://{temp_path}",
            ]
        elif spec["kind"] == "delete-role-policy":
            command = [
                "aws", "--profile", profile, "--region", "eu-west-2", "--no-cli-pager",
                "iam", "delete-role-policy", "--role-name", spec["role"],
                "--policy-name", spec["policy_name"],
            ]
        else:
            command = [
                "aws", "--profile", profile, "--region", "eu-west-2", "--no-cli-pager",
                "iam", "delete-role", "--role-name", spec["role"],
            ]
        completed = runner(command, env=env)
        return getattr(completed, "returncode", 1)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=sorted(ACTIONS))
    parser.add_argument("--profile", default="focused-bootstrap")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_FILE)
    parser.add_argument("--receipt", type=Path, default=RECEIPT_FILE)
    parser.add_argument("--rendered-dir", type=Path, default=RENDERED_DIR)
    parser.add_argument("--expected-account", default="")
    args = parser.parse_args()
    expected = args.expected_account or _expected_account()
    if not expected:
        print("expected account is required", file=sys.stderr)
        return 2
    try:
        return run_action(
            action=args.action,
            manifest_path=args.manifest,
            rendered_dir=args.rendered_dir,
            receipt_path=args.receipt,
            profile=args.profile,
            expected_account=expected,
        )
    except Exception as exc:
        print(f"iam_action_failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
