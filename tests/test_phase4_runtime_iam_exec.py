"""Tests for the runtime IAM saved-plan/apply receipt and scope guard."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import phase4_iam_manifest as manifest_builder  # noqa: E402
import phase4_review_receipt as receipt_module  # noqa: E402
import phase4_runtime_iam_exec as runtime_exec  # noqa: E402

ALLOWED = [
    "terraform_data.account_guard",
    "aws_iam_policy.runtime_boundary",
    "aws_iam_role.instance",
    "aws_iam_role_policy.logs",
    "aws_iam_role_policy.ecr_pull",
    "aws_iam_role_policy_attachment.ssm",
    "aws_iam_instance_profile.app",
]


def _document(forbidden: bool = False) -> dict:
    addresses = ALLOWED + (["aws_ecr_repository.app"] if forbidden else [])
    return {
        "resource_changes": [
            {"address": address, "change": {"actions": ["create"]}} for address in addresses
        ]
    }


class FakeRunner:
    def __init__(self, *, forbidden: bool = False):
        self.forbidden = forbidden
        self.calls = []
        self.var_file = None
        self.var_payload = None
        self.show_payload = json.dumps(_document(forbidden))

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        if "plan" in command and "show" not in command:
            for item in command:
                if item.startswith("-out="):
                    Path(item.removeprefix("-out=")).write_bytes(b"plan-bytes")
                if item.startswith("-var-file="):
                    self.var_file = Path(item.split("=", 1)[1])
                    self.var_payload = json.loads(
                        self.var_file.read_text(encoding="utf-8")
                    )
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if "show" in command:
            return type("Result", (), {"returncode": 0, "stdout": self.show_payload, "stderr": ""})()
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path, dict]:
    rendered = tmp_path / "iam"
    rendered.mkdir(parents=True)
    for name in manifest_builder.ACTIVE:
        (rendered / name).write_bytes(b'{"Version":"2012-10-17","Statement":[]}')
    (rendered / "runtime-boundary-policy.json").write_bytes(b'{"boundary":true}')
    terraform = tmp_path / "terraform"
    terraform.mkdir()
    (terraform / "main.tf").write_bytes(b"terraform source")
    manifest = rendered / "active-manifest.json"
    document = manifest_builder.build_manifest(rendered, terraform / "main.tf")
    manifest.write_text(json.dumps(document), encoding="utf-8")
    receipt = tmp_path / "independent-review-receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "schema": receipt_module.SCHEMA,
                "reviewer": "synthetic-test-codex",
                "reviewed_at_utc": "2026-11-04T00:00:00Z",
                "decision": "accepted",
                "scope": receipt_module.SCOPE,
                "manifest_schema": document["schema"],
                "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "active_document_count": document["active_document_count"],
                "terraform_source_sha256": document["terraform_generated_source"]["sha256"],
            }
        ),
        encoding="utf-8",
    )
    return rendered, manifest, receipt, terraform, tmp_path / "phase4.tfplan", document


def _add_plan_receipt(receipt_path: Path, plan_path: Path, payload: str) -> None:
    value = json.loads(receipt_path.read_text(encoding="utf-8"))
    value["runtime_iam_plan"] = {
        "reviewed_at_utc": "2026-11-04T00:00:00Z",
        "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "plan_json_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }
    receipt_path.write_text(json.dumps(value), encoding="utf-8")


def test_plan_submits_verified_boundary_and_writes_review_artifacts(tmp_path):
    rendered, manifest, receipt, terraform, plan, _ = _fixture(tmp_path)
    runner = FakeRunner()
    assert runtime_exec.run_plan(
        manifest_path=manifest, rendered_dir=rendered, receipt_path=receipt,
        terraform_dir=terraform, plan_path=plan, plan_json_path=tmp_path / "phase4.json",
        runner=runner,
    ) == 0
    assert runner.var_file is not None
    assert runner.var_payload == {
        "runtime_boundary_policy_json": '{"boundary":true}'
    }
    assert not runner.var_file.exists()
    assert (tmp_path / "phase4.json").is_file()
    assert not any(
        "runtime_boundary_policy_path" in argument
        for call in runner.calls
        for argument in call
    )


def test_missing_or_changed_receipt_prevents_plan(tmp_path):
    rendered, manifest, receipt, terraform, plan, _ = _fixture(tmp_path)
    receipt.unlink()
    runner = FakeRunner()
    with pytest.raises(Exception):
        runtime_exec.run_plan(
            manifest_path=manifest, rendered_dir=rendered, receipt_path=receipt,
            terraform_dir=terraform, plan_path=plan, plan_json_path=tmp_path / "out.json",
            runner=runner,
        )
    assert runner.calls == []


def test_altered_boundary_or_source_prevents_plan(tmp_path):
    rendered, manifest, receipt, terraform, plan, _ = _fixture(tmp_path)
    (rendered / "runtime-boundary-policy.json").write_bytes(b'{"changed":true}')
    runner = FakeRunner()
    with pytest.raises(Exception):
        runtime_exec.run_plan(
            manifest_path=manifest, rendered_dir=rendered, receipt_path=receipt,
            terraform_dir=terraform, plan_path=plan, plan_json_path=tmp_path / "out.json",
            runner=runner,
        )
    assert runner.calls == []


def test_missing_boundary_prevents_plan(tmp_path):
    rendered, manifest, receipt, terraform, plan, _ = _fixture(tmp_path)
    (rendered / "runtime-boundary-policy.json").unlink()
    runner = FakeRunner()
    with pytest.raises(Exception):
        runtime_exec.run_plan(
            manifest_path=manifest, rendered_dir=rendered, receipt_path=receipt,
            terraform_dir=terraform, plan_path=plan, plan_json_path=tmp_path / "out.json",
            runner=runner,
        )
    assert runner.calls == []


def test_apply_derives_json_from_binary_and_requires_matching_receipt(tmp_path):
    rendered, manifest, receipt, terraform, plan, _ = _fixture(tmp_path)
    runner = FakeRunner()
    assert runtime_exec.run_plan(
        manifest_path=manifest, rendered_dir=rendered, receipt_path=receipt,
        terraform_dir=terraform, plan_path=plan, plan_json_path=tmp_path / "out.json",
        runner=runner,
    ) == 0
    _add_plan_receipt(receipt, plan, runner.show_payload)
    apply_runner = FakeRunner()
    assert runtime_exec.run_apply(
        plan_path=plan, manifest_path=manifest, rendered_dir=rendered,
        receipt_path=receipt, runner=apply_runner,
    ) == 0
    assert apply_runner.calls and "show" in apply_runner.calls[0]
    assert apply_runner.calls[-1][-1] == str(plan)


def test_replacement_binary_with_matching_sidecar_and_old_json_is_rejected(tmp_path):
    rendered, manifest, receipt, terraform, plan, _ = _fixture(tmp_path)
    runner = FakeRunner()
    assert runtime_exec.run_plan(
        manifest_path=manifest, rendered_dir=rendered, receipt_path=receipt,
        terraform_dir=terraform, plan_path=plan, plan_json_path=tmp_path / "out.json",
        runner=runner,
    ) == 0
    _add_plan_receipt(receipt, plan, runner.show_payload)
    plan.write_bytes(b"replacement-binary")
    plan.with_suffix(".tfplan.sha256").write_text(
        hashlib.sha256(b"replacement-binary").hexdigest() + "\n", encoding="utf-8"
    )
    apply_runner = FakeRunner()
    with pytest.raises(Exception):
        runtime_exec.run_apply(
            plan_path=plan, manifest_path=manifest, rendered_dir=rendered,
            receipt_path=receipt, runner=apply_runner,
        )
    assert apply_runner.calls == []


def test_forbidden_derived_plan_prevents_apply(tmp_path):
    rendered, manifest, receipt, terraform, plan, _ = _fixture(tmp_path)
    runner = FakeRunner()
    assert runtime_exec.run_plan(
        manifest_path=manifest, rendered_dir=rendered, receipt_path=receipt,
        terraform_dir=terraform, plan_path=plan, plan_json_path=tmp_path / "out.json",
        runner=runner,
    ) == 0
    forbidden_runner = FakeRunner(forbidden=True)
    _add_plan_receipt(receipt, plan, forbidden_runner.show_payload)
    apply_runner = FakeRunner(forbidden=True)
    with pytest.raises(Exception):
        runtime_exec.run_apply(
            plan_path=plan, manifest_path=manifest, rendered_dir=rendered,
            receipt_path=receipt, runner=apply_runner,
        )
    assert all("apply" not in call for call in apply_runner.calls)
