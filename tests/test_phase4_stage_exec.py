"""Behavioral tests for verified-command execution with scoped environments."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import phase4_stage_exec as stage_exec  # noqa: E402

ACCOUNT = "123456789012"
DEPLOY_ROLE = "focused-delivery-recovery-deploy"


class FakeRunner:
    def __init__(
        self,
        *,
        identity: dict | None = None,
        identity_returncode: int = 0,
        identity_stdout: str | None = None,
        command_returncode: int = 0,
    ) -> None:
        self.identity = identity or {
            "Account": ACCOUNT,
            "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/{DEPLOY_ROLE}/phase4",
        }
        self.identity_returncode = identity_returncode
        self.identity_stdout = (
            json.dumps(self.identity) if identity_stdout is None else identity_stdout
        )
        self.command_returncode = command_returncode
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), dict(kwargs)))
        if "get-caller-identity" in command:
            return subprocess.CompletedProcess(
                command,
                self.identity_returncode,
                stdout=self.identity_stdout,
                stderr="",
            )
        return subprocess.CompletedProcess(
            command, self.command_returncode, stdout="", stderr=""
        )


def _run(runner: FakeRunner, *, base_env=None, standard_chain=False, command=None):
    return stage_exec.run_scoped(
        stage="deploy-test",
        profile="focused-deploy",
        expected_account=ACCOUNT,
        expected_role=DEPLOY_ROLE,
        command=(
            ["aws", "ecr", "describe-images", "--repository-name", "x"]
            if command is None
            else command
        ),
        standard_chain=standard_chain,
        base_env=base_env,
        runner=runner,
    )


def test_correct_scoped_stage_runs_command_in_sanitized_environment():
    runner = FakeRunner()
    result = _run(runner)
    assert result == 0
    assert len(runner.calls) == 2
    identity_command, identity_kwargs = runner.calls[0]
    command, command_kwargs = runner.calls[1]
    assert "get-caller-identity" in identity_command
    assert command == [
        "aws",
        "ecr",
        "describe-images",
        "--repository-name",
        "x",
    ]
    assert command_kwargs["env"]["AWS_PROFILE"] == "focused-deploy"
    assert "AWS_ACCESS_KEY_ID" not in command_kwargs["env"]


def test_stale_parent_default_and_conflicting_credentials_are_not_inherited():
    runner = FakeRunner()
    base = {
        "AWS_PROFILE": "default",
        "AWS_ACCESS_KEY_ID": "secret-key",
        "AWS_SECRET_ACCESS_KEY": "secret-secret",
        "AWS_SESSION_TOKEN": "secret-token",
        "KEEP": "yes",
    }
    result = _run(runner, base_env=base)
    assert result == 0
    env = runner.calls[1][1]["env"]
    assert env["AWS_PROFILE"] == "focused-deploy"
    for key in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        assert key not in env
    assert env["KEEP"] == "yes"


def test_wrong_role_prevents_command_invocation(capsys):
    runner = FakeRunner(
        identity={
            "Account": ACCOUNT,
            "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/focused-delivery-recovery-bootstrap/phase4",
        }
    )
    result = _run(runner)
    assert result == 1
    assert len(runner.calls) == 1
    assert "command_not_run=true" in capsys.readouterr().err


def test_wrong_account_prevents_command_invocation():
    runner = FakeRunner(
        identity={
            "Account": "999999999999",
            "Arn": f"arn:aws:sts::999999999999:assumed-role/{DEPLOY_ROLE}/phase4",
        }
    )
    assert _run(runner) == 1
    assert len(runner.calls) == 1


def test_failed_identity_lookup_prevents_command_invocation(capsys):
    runner = FakeRunner(identity_returncode=255)
    assert _run(runner) == 1
    assert len(runner.calls) == 1
    assert "identity_lookup_failed=true" in capsys.readouterr().err


def test_invalid_identity_json_prevents_command_invocation():
    runner = FakeRunner(identity_stdout="{not-json")
    assert _run(runner) == 1
    assert len(runner.calls) == 1


def test_native_command_failure_is_returned_and_not_masked():
    runner = FakeRunner(command_returncode=7)
    assert _run(runner) == 7
    assert len(runner.calls) == 2


def test_standard_chain_keeps_runner_credentials_but_removes_profile():
    runner = FakeRunner()
    base = {
        "AWS_PROFILE": "default",
        "AWS_ACCESS_KEY_ID": "runner-key",
        "AWS_SECRET_ACCESS_KEY": "runner-secret",
        "AWS_SESSION_TOKEN": "runner-token",
    }
    assert _run(runner, base_env=base, standard_chain=True) == 0
    env = runner.calls[1][1]["env"]
    assert "AWS_PROFILE" not in env
    assert env["AWS_ACCESS_KEY_ID"] == "runner-key"
    assert env["AWS_SESSION_TOKEN"] == "runner-token"


def test_missing_command_does_not_invoke_runner():
    runner = FakeRunner()
    result = _run(runner, command=[])
    assert result == 2
    assert runner.calls == []


def test_runbooks_route_cli_and_terraform_through_the_stage_runner():
    text = (REPO / "docs/operations/REFERENCE_OPERATIONS.md").read_text(encoding="utf-8")
    for term in ("phase4_stage_exec.py", "if ($LASTEXITCODE -ne 0)", "expected account", "expected role", "same child process"):
        assert term in text
