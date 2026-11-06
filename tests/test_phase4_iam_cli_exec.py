"""Tests for the fixed-action deploy/operator IAM CLI executor."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import phase4_iam_cli_exec as cli_exec  # noqa: E402
import phase4_iam_manifest as manifest_builder  # noqa: E402
import phase4_review_receipt as receipt_module  # noqa: E402

ACCOUNT = "123456789012"
BOOTSTRAP = "focused-delivery-recovery-bootstrap"


class FakeRunner:
    def __init__(self, *, account: str = ACCOUNT, role: str = BOOTSTRAP, mutation_code: int = 0):
        self.account = account
        self.role = role
        self.mutation_code = mutation_code
        self.calls: list[tuple[list[str], bytes | None]] = []

    def __call__(self, command, **kwargs):
        if "get-caller-identity" in command:
            return type(
                "Result",
                (),
                {
                    "returncode": 0,
                    "stdout": json.dumps(
                        {
                            "Account": self.account,
                            "Arn": f"arn:aws:sts::{self.account}:assumed-role/{self.role}/phase4",
                        }
                    ),
                    "stderr": "",
                },
            )()
        payload = None
        for index, item in enumerate(command):
            if item.startswith("file://"):
                payload = Path(item.removeprefix("file://")).read_bytes()
        self.calls.append((list(command), payload))
        return type("Result", (), {"returncode": self.mutation_code, "stdout": "", "stderr": ""})()


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    rendered = tmp_path / "iam"
    rendered.mkdir()
    for name in manifest_builder.ACTIVE:
        (rendered / name).write_bytes(b'{"Version":"2012-10-17","Statement":[]}')
    (rendered / "deploy-named-profile-trust-policy.json").write_bytes(b'{"deploy":true}')
    (rendered / "deploy-policy.json").write_bytes(b'{"deployPolicy":true}')
    main = tmp_path / "main.tf"
    main.write_bytes(b"terraform")
    manifest = rendered / "active-manifest.json"
    document = manifest_builder.build_manifest(rendered, main)
    manifest.write_text(json.dumps(document), encoding="utf-8")
    receipt = tmp_path / "independent-review-receipt.json"
    receipt.write_text(json.dumps({
        "schema": receipt_module.SCHEMA,
        "reviewer": "synthetic-test-codex",
        "reviewed_at_utc": "2026-11-04T00:00:00Z",
        "decision": "accepted",
        "scope": receipt_module.SCOPE,
        "manifest_schema": document["schema"],
        "manifest_sha256": __import__("hashlib").sha256(manifest.read_bytes()).hexdigest(),
        "active_document_count": document["active_document_count"],
        "terraform_source_sha256": document["terraform_generated_source"]["sha256"],
    }), encoding="utf-8")
    return rendered, manifest, receipt


def _run(rendered: Path, manifest: Path, receipt: Path, runner: FakeRunner, action: str = "create-deploy"):
    return cli_exec.run_action(
        action=action,
        manifest_path=manifest,
        rendered_dir=rendered,
        receipt_path=receipt,
        profile="focused-bootstrap",
        expected_account=ACCOUNT,
        runner=runner,
    )


def test_create_deploy_submits_exact_verified_bytes(tmp_path):
    rendered, manifest, receipt = _fixture(tmp_path)
    runner = FakeRunner()
    assert _run(rendered, manifest, receipt, runner) == 0
    assert len(runner.calls) == 1
    command, payload = runner.calls[0]
    assert "create-role" in command
    assert "focused-delivery-recovery-deploy" in command
    assert payload == b'{"deploy":true}'


def test_put_policy_submits_exact_verified_bytes(tmp_path):
    rendered, manifest, receipt = _fixture(tmp_path)
    runner = FakeRunner()
    assert _run(rendered, manifest, receipt, runner, action="put-deploy-policy") == 0
    _, payload = runner.calls[0]
    assert payload == b'{"deployPolicy":true}'


def test_altered_input_prevents_identity_and_mutation(tmp_path):
    rendered, manifest, receipt = _fixture(tmp_path)
    (rendered / "deploy-named-profile-trust-policy.json").write_bytes(b'{"changed":true}')
    runner = FakeRunner()
    with pytest.raises(Exception):
        _run(rendered, manifest, receipt, runner)
    assert runner.calls == []


def test_wrong_role_or_account_prevents_mutation(tmp_path):
    rendered, manifest, receipt = _fixture(tmp_path)
    runner = FakeRunner(role="focused-delivery-recovery-deploy")
    with pytest.raises(RuntimeError):
        _run(rendered, manifest, receipt, runner)
    assert runner.calls == []

    runner = FakeRunner(account="999999999999")
    with pytest.raises(RuntimeError):
        _run(rendered, manifest, receipt, runner)
    assert runner.calls == []


def test_delete_action_requires_identity_but_no_document(tmp_path):
    rendered, manifest, receipt = _fixture(tmp_path)
    runner = FakeRunner()
    assert _run(rendered, manifest, receipt, runner, action="delete-deploy-role") == 0
    command, payload = runner.calls[0]
    assert "delete-role" in command
    assert payload is None


def test_native_failure_and_unapproved_action_are_not_hidden(tmp_path):
    rendered, manifest, receipt = _fixture(tmp_path)
    runner = FakeRunner(mutation_code=7)
    assert _run(rendered, manifest, receipt, runner) == 7
    with pytest.raises(RuntimeError):
        _run(rendered, manifest, receipt, FakeRunner(), action="create-admin")
