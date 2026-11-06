"""Verify the named stage identity for local checks.

This module builds a sanitized child environment for its own AWS CLI lookup. It
does not and cannot change the caller's PowerShell environment. Use
``phase4_stage_exec.py`` when a command must run under the verified stage
identity; that wrapper verifies and executes in the same sanitized child
environment. It never prints credentials or the full caller ARN.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess

ASSUMED_ROLE = re.compile(r"^arn:aws[a-z-]*:sts::(\d{12}):assumed-role/([^/]+)/")
CONFLICTING_PROFILE_CREDENTIALS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_CREDENTIAL_EXPIRY",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
)


def _profile_env(profile: str, base: dict[str, str] | None = None, region: str = "eu-west-2") -> dict[str, str]:
    """Build a named-profile environment with conflicting temporary creds removed.

    Values are never logged. Removing these environment variables prevents a
    stale shell export from overriding the reviewed stage profile.
    """
    env = dict(base if base is not None else os.environ)
    for key in CONFLICTING_PROFILE_CREDENTIALS:
        env.pop(key, None)
    env["AWS_PROFILE"] = profile
    env["AWS_REGION"] = region
    env["AWS_DEFAULT_REGION"] = region
    return env


def _standard_env(base: dict[str, str] | None = None, region: str = "eu-west-2") -> dict[str, str]:
    """Keep the short-lived standard chain but remove an accidental profile."""
    env = dict(base if base is not None else os.environ)
    env.pop("AWS_PROFILE", None)
    env.pop("AWS_DEFAULT_PROFILE", None)
    env["AWS_REGION"] = region
    env["AWS_DEFAULT_REGION"] = region
    return env


def _identity(profile: str, *, standard_chain: bool, region: str) -> dict:
    if standard_chain:
        env = _standard_env(region=region)
        command = ["aws", "--region", region, "--no-cli-pager", "sts", "get-caller-identity", "--output", "json"]
    else:
        env = _profile_env(profile, region=region)
        command = [
            "aws",
            "--profile",
            profile,
            "--region",
            region,
            "--no-cli-pager",
            "sts",
            "get-caller-identity",
            "--output",
            "json",
        ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=60, env=env)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip())
    return json.loads(completed.stdout or "{}")


def verify_identity(identity: dict, expected_account: str, expected_role: str) -> dict:
    arn = str(identity.get("Arn", ""))
    match = ASSUMED_ROLE.match(arn)
    actual_account = identity.get("Account") or (match.group(1) if match else None)
    actual_role = match.group(2) if match else None
    return {
        "account_match": bool(actual_account and actual_account == expected_account),
        "role_match": bool(actual_role and actual_role == expected_role),
        "principal_kind": "assumed-role" if match else "other",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True)
    parser.add_argument("--profile", default="")
    parser.add_argument("--standard-chain", action="store_true")
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--expected-role", required=True)
    parser.add_argument("--region", default="eu-west-2")
    args = parser.parse_args()
    if not args.standard_chain and not args.profile:
        raise SystemExit("a stage profile or --standard-chain is required")
    identity = _identity(args.profile, standard_chain=args.standard_chain, region=args.region)
    result = verify_identity(identity, args.expected_account, args.expected_role)
    print(
        f"stage={args.stage} account_match={str(result['account_match']).lower()} "
        f"role_match={str(result['role_match']).lower()} principal={result['principal_kind']}"
    )
    return 0 if result["account_match"] and result["role_match"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
