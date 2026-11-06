"""Tests for the saved runtime-IAM plan scope guard."""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import phase4_iam_plan_scope as scope  # noqa: E402


def _change(address: str, *actions: str) -> dict:
    return {"address": address, "change": {"actions": list(actions or ("create",))}}


def test_expected_runtime_iam_group_is_allowed():
    document = {"resource_changes": [_change(address) for address in sorted(scope.ALLOWED)]}
    assert scope.validate_plan(document) == []


def test_ecr_repository_or_compute_pulled_into_plan_is_rejected():
    document = {
        "resource_changes": [
            _change("aws_iam_role.instance"),
            _change("aws_ecr_repository.app"),
        ]
    }
    errors = scope.validate_plan(document)
    assert any("aws_ecr_repository.app" in error for error in errors)
    assert any("forbidden resource class" in error for error in errors)


def test_instance_volume_logs_network_are_rejected():
    for address in (
        "aws_instance.app",
        "aws_ebs_volume.data",
        "aws_cloudwatch_log_group.app",
        "aws_security_group.app",
    ):
        document = {"resource_changes": [_change(address)]}
        assert scope.validate_plan(document)


def test_destroy_or_empty_plan_is_rejected():
    destroy = {"resource_changes": [_change("aws_iam_role.instance", "delete")]}
    assert scope.validate_plan(destroy)
    assert scope.validate_plan({"resource_changes": []})
