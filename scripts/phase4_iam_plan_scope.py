"""Validate that a saved Terraform plan contains only the runtime IAM group."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ALLOWED = {
    "terraform_data.account_guard",
    "aws_iam_policy.runtime_boundary",
    "aws_iam_role.instance",
    "aws_iam_role_policy.logs",
    "aws_iam_role_policy.ecr_pull",
    "aws_iam_role_policy_attachment.ssm",
    "aws_iam_instance_profile.app",
}
FORBIDDEN_PREFIXES = (
    "aws_ecr_",
    "aws_instance",
    "aws_ebs_",
    "aws_volume_",
    "aws_cloudwatch_",
    "aws_security_group",
    "aws_vpc",
    "aws_subnet",
    "aws_route",
    "aws_nat_",
    "aws_lb",
    "aws_lb_",
)


def validate_plan(document: dict) -> list[str]:
    errors: list[str] = []
    for change in document.get("resource_changes", []) or []:
        address = change.get("address", "")
        actions = change.get("change", {}).get("actions", [])
        if address not in ALLOWED:
            errors.append(f"unexpected resource change: {address}")
        if any(action in {"delete", "create_before_destroy"} for action in actions):
            errors.append(f"unexpected action {actions} for {address}")
        if address.startswith(FORBIDDEN_PREFIXES):
            errors.append(f"forbidden resource class: {address}")
    if not document.get("resource_changes"):
        errors.append("plan contains no resource_changes")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("plan_json", type=Path)
    args = parser.parse_args()
    document = json.loads(args.plan_json.read_text(encoding="utf-8"))
    errors = validate_plan(document)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(f"runtime_iam_plan_scope_ok resources={len(document.get('resource_changes', []))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
