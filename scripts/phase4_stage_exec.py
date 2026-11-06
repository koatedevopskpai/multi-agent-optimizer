"""Verify a Phase 4 stage identity and execute the command in the same env.

This wrapper sanitizes its own child environment, verifies the assumed role and
account, then runs the approved command with that environment. It cannot and
does not change the caller's PowerShell process. Never prints credentials or the
caller ARN.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from phase4_stage_preflight import _profile_env, _standard_env, verify_identity


def _identity(
    profile: str,
    *,
    standard_chain: bool,
    region: str,
    env: dict[str, str],
    runner,
) -> dict | None:
    if standard_chain:
        command = [
            "aws",
            "--region",
            region,
            "--no-cli-pager",
            "sts",
            "get-caller-identity",
            "--output",
            "json",
        ]
    else:
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
    completed = runner(command, capture_output=True, text=True, timeout=60, env=env)
    if getattr(completed, "returncode", 1) != 0:
        return None
    try:
        return json.loads(getattr(completed, "stdout", "") or "{}")
    except json.JSONDecodeError:
        return None


def run_scoped(
    *,
    stage: str,
    profile: str,
    expected_account: str,
    expected_role: str,
    command: list[str],
    standard_chain: bool = False,
    region: str = "eu-west-2",
    quiet: bool = False,
    base_env: dict[str, str] | None = None,
    runner=subprocess.run,
) -> int:
    """Verify identity, then execute ``command`` with the same sanitized env."""
    if not command:
        print(f"stage={stage} command_missing=true", file=sys.stderr)
        return 2
    env = (
        _standard_env(base_env, region=region)
        if standard_chain
        else _profile_env(profile, base_env, region)
    )
    identity = _identity(
        profile,
        standard_chain=standard_chain,
        region=region,
        env=env,
        runner=runner,
    )
    if identity is None:
        print(f"stage={stage} identity_lookup_failed=true", file=sys.stderr)
        return 1
    result = verify_identity(identity, expected_account, expected_role)
    if not (result["account_match"] and result["role_match"]):
        print(
            f"stage={stage} account_match={str(result['account_match']).lower()} "
            f"role_match={str(result['role_match']).lower()} "
            f"principal={result['principal_kind']}; command_not_run=true",
            file=sys.stderr,
        )
        return 1
    if not quiet:
        print(
            f"stage={stage} account_match=true role_match=true "
            f"principal={result['principal_kind']}"
        )
    completed = runner(command, env=env)
    return getattr(completed, "returncode", 1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True)
    parser.add_argument("--profile", default="")
    parser.add_argument("--standard-chain", action="store_true")
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--expected-role", required=True)
    parser.add_argument("--region", default="eu-west-2")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not args.standard_chain and not args.profile:
        print("a stage profile or --standard-chain is required", file=sys.stderr)
        return 2
    return run_scoped(
        stage=args.stage,
        profile=args.profile,
        expected_account=args.expected_account,
        expected_role=args.expected_role,
        command=command,
        standard_chain=args.standard_chain,
        region=args.region,
        quiet=args.quiet,
    )


if __name__ == "__main__":
    raise SystemExit(main())
