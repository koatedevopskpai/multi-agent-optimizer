"""Behavioral tests for explicit Phase 4 stage identities."""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import phase4_stage_preflight as preflight  # noqa: E402


def _tfvars(name: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (REPO / "infra" / "terraform" / name).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def test_stage_files_pin_distinct_profiles_and_roles():
    bootstrap = _tfvars("terraform.bootstrap.tfvars.example")
    deploy = _tfvars("terraform.deploy.tfvars.example")
    oidc = _tfvars("terraform.deploy-oidc.tfvars.example")
    assert bootstrap == {
        "aws_profile": '"focused-bootstrap"',
        "expected_stage_role_name": '"focused-delivery-recovery-bootstrap"',
    }
    assert deploy == {
        "aws_profile": '"focused-deploy"',
        "expected_stage_role_name": '"focused-delivery-recovery-deploy"',
    }
    assert oidc == {
        "aws_profile": '""',
        "expected_stage_role_name": '"focused-delivery-recovery-deploy"',
    }


def test_common_tfvars_does_not_override_stage_profile():
    common = _tfvars("terraform.tfvars.example")
    assert "aws_profile" not in common
    assert "expected_stage_role_name" not in common
    assert "expected_account_id" in common


def test_terraform_guard_checks_role_and_account():
    main = (REPO / "infra/terraform/main.tf").read_text(encoding="utf-8")
    variables = (REPO / "infra/terraform/variables.tf").read_text(encoding="utf-8")
    assert "expected_stage_role_name" in variables
    assert ":assumed-role/${var.expected_stage_role_name}/" in main
    assert "expected_account_id" in main


def test_matching_assumed_role_and_account_pass():
    identity = {
        "Account": "123456789012",
        "Arn": "arn:aws:sts::123456789012:assumed-role/focused-delivery-recovery-deploy/phase4",
    }
    result = preflight.verify_identity(
        identity, "123456789012", "focused-delivery-recovery-deploy"
    )
    assert result == {
        "account_match": True,
        "role_match": True,
        "principal_kind": "assumed-role",
    }


def test_wrong_role_wrong_account_and_user_principal_fail():
    wrong_role = {
        "Account": "123456789012",
        "Arn": "arn:aws:sts::123456789012:assumed-role/focused-delivery-recovery-bootstrap/phase4",
    }
    wrong_account = {
        "Account": "999999999999",
        "Arn": "arn:aws:sts::999999999999:assumed-role/focused-delivery-recovery-deploy/phase4",
    }
    user = {
        "Account": "123456789012",
        "Arn": "arn:aws:iam::123456789012:user/admin",
    }
    expected = ("123456789012", "focused-delivery-recovery-deploy")
    assert preflight.verify_identity(wrong_role, *expected)["role_match"] is False
    assert preflight.verify_identity(wrong_account, *expected)["account_match"] is False
    assert preflight.verify_identity(user, *expected) == {
        "account_match": True,
        "role_match": False,
        "principal_kind": "other",
    }


def test_profile_environment_removes_conflicting_temporary_credentials():
    base = {
        "AWS_ACCESS_KEY_ID": "secret-access-key",
        "AWS_SECRET_ACCESS_KEY": "secret-secret-key",
        "AWS_SESSION_TOKEN": "secret-session-token",
        "AWS_SECURITY_TOKEN": "secret-security-token",
        "AWS_CREDENTIAL_EXPIRY": "2099-01-01T00:00:00Z",
        "AWS_PROFILE": "default",
        "AWS_DEFAULT_PROFILE": "default",
        "KEEP": "yes",
    }
    env = preflight._profile_env("focused-deploy", base=base)
    for key in preflight.CONFLICTING_PROFILE_CREDENTIALS:
        if key == "AWS_PROFILE":
            continue
        assert key not in env
    assert env["AWS_PROFILE"] == "focused-deploy"
    assert env["AWS_REGION"] == "eu-west-2"
    assert env["KEEP"] == "yes"
    assert "secret" not in " ".join(env.values())


def test_standard_chain_keeps_runner_credentials_but_removes_profile():
    base = {
        "AWS_ACCESS_KEY_ID": "runner-key",
        "AWS_SECRET_ACCESS_KEY": "runner-secret",
        "AWS_SESSION_TOKEN": "runner-token",
        "AWS_PROFILE": "default",
        "AWS_DEFAULT_PROFILE": "default",
    }
    env = preflight._standard_env(base=base)
    assert env["AWS_ACCESS_KEY_ID"] == "runner-key"
    assert env["AWS_SESSION_TOKEN"] == "runner-token"
    assert "AWS_PROFILE" not in env
    assert "AWS_DEFAULT_PROFILE" not in env


def test_public_operations_uses_stage_exec_and_exit_guards():
    text = (REPO / "docs/operations/REFERENCE_OPERATIONS.md").read_text(encoding="utf-8")
    assert "phase4_stage_exec.py" in text
    assert "terraform.bootstrap.tfvars" in text
    assert "terraform.deploy.tfvars" in text
    assert "if ($LASTEXITCODE -ne 0)" in text
    assert "same child process" in text


def test_deploy_runbook_has_valid_build_tag_push_digest_sequence():
    text = (REPO / "docs/operations/REFERENCE_OPERATIONS.md").read_text(encoding="utf-8")
    tag = text.index("docker tag delivery-recovery:local")
    push = text.index('docker push "${Registry}/focused-delivery-recovery:${Tag}"')
    readback = text.index("aws ecr describe-images")
    assert tag < push < readback
    assert "imageTag=$Tag" in text
    assert "imageDetails[0].imageDigest" in text
