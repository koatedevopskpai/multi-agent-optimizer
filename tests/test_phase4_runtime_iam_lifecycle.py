"""AWS-free real-Terraform regression for the saved-plan input lifecycle.

These tests use built-in ``terraform_data`` only. They do not require AWS
credentials, MFA or any cloud provider.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import phase4_iam_manifest as manifest_builder  # noqa: E402
import phase4_review_receipt as receipt_module  # noqa: E402
import phase4_runtime_iam_exec as runtime_exec  # noqa: E402

TERRAFORM = shutil.which("terraform")
pytestmark = pytest.mark.skipif(TERRAFORM is None, reason="terraform is not installed")

POLICY = (
    '{"Statement":[{"Action":["ssm:UpdateInstanceInformation"],'
    '"Effect":"Allow","Resource":"*","Sid":"SystemsManagerRuntimeChannels"}],'
    '"Version":"2012-10-17"}'
)

TEMPLATE_POLICY = (
    '{"Statement":[{"Action":["ssm:UpdateInstanceInformation"],'
    '"Effect":"Allow","Resource":"arn:aws:iam::${account_id}:root",'
    '"Sid":"SystemsManagerRuntimeChannels"}],"Version":"2012-10-17"}'
)

OLD_MAIN = """
variable "policy_path" { type = string }

resource "terraform_data" "account_guard" {
  input = templatefile(var.policy_path, { account_id = "123456789012" })
}

output "policy" { value = terraform_data.account_guard.output }
"""

NEW_MAIN = """
variable "runtime_boundary_policy_json" { type = string }

resource "terraform_data" "account_guard" {
  input = var.runtime_boundary_policy_json
}

output "policy" { value = terraform_data.account_guard.output }
"""


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["TF_IN_AUTOMATION"] = "1"
    env["TF_INPUT"] = "0"
    env["TF_CLI_ARGS"] = ""
    return env


def _tf(*args: str, cwd: Path, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        [TERRAFORM, *args],
        cwd=cwd,
        env=_env(),
        capture_output=capture,
        text=True,
    )


def _init(module: Path) -> None:
    completed = _tf("init", "-input=false", "-no-color", cwd=module)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _fixture(tmp_path: Path, main_tf: str) -> tuple[Path, Path, Path, Path]:
    rendered = tmp_path / "iam"
    rendered.mkdir(parents=True)
    for name in manifest_builder.ACTIVE:
        (rendered / name).write_text(
            '{"Version":"2012-10-17","Statement":[]}', encoding="utf-8"
        )
    (rendered / "runtime-boundary-policy.json").write_text(POLICY, encoding="utf-8")
    terraform = tmp_path / "terraform"
    terraform.mkdir()
    (terraform / "main.tf").write_text(main_tf, encoding="utf-8")
    manifest = rendered / "active-manifest.json"
    document = manifest_builder.build_manifest(rendered, terraform / "main.tf")
    manifest.write_text(json.dumps(document), encoding="utf-8")
    receipt = tmp_path / "receipt.json"
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
    return rendered, manifest, receipt, terraform


def _add_plan_receipt(receipt_path: Path, plan_path: Path, payload: str) -> None:
    value = json.loads(receipt_path.read_text(encoding="utf-8"))
    value["runtime_iam_plan"] = {
        "reviewed_at_utc": "2026-11-04T00:00:00Z",
        "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "plan_json_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }
    receipt_path.write_text(json.dumps(value), encoding="utf-8")


def test_reproduces_deleted_template_file_failure(tmp_path):
    """The pre-fix pattern fails when the transient template file is removed."""
    module = tmp_path / "old"
    module.mkdir()
    (module / "main.tf").write_text(OLD_MAIN, encoding="utf-8")
    transient = tmp_path / "boundary.json"
    transient.write_text(TEMPLATE_POLICY, encoding="utf-8")
    _init(module)

    plan = tmp_path / "old.tfplan"
    planned = _tf(
        "plan",
        "-input=false",
        "-no-color",
        f"-var=policy_path={transient}",
        f"-out={plan}",
        cwd=module,
    )
    assert planned.returncode == 0, planned.stdout + planned.stderr

    transient.unlink()
    applied = _tf("apply", "-input=false", "-no-color", str(plan), cwd=module)

    assert applied.returncode != 0
    output = (applied.stdout + applied.stderr).lower()
    assert "no file exists" in output or "invalid function argument" in output


def test_saved_plan_applies_after_transient_input_removed(tmp_path, monkeypatch):
    """The corrected wrapper must bake the verified policy value into the plan."""
    rendered, manifest, receipt, terraform = _fixture(tmp_path, NEW_MAIN)
    _init(terraform)
    monkeypatch.setattr(
        runtime_exec, "_plan_targets", lambda: ["-target=terraform_data.account_guard"]
    )

    real_mkstemp = runtime_exec.tempfile.mkstemp
    transient_paths: list[Path] = []

    def recording_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        transient_paths.append(Path(name))
        return fd, name

    monkeypatch.setattr(runtime_exec.tempfile, "mkstemp", recording_mkstemp)

    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(list(command))
        return subprocess.run(command, **kwargs)

    plan = tmp_path / "runtime.tfplan"
    plan_json = tmp_path / "runtime.json"
    assert (
        runtime_exec.run_plan(
            manifest_path=manifest,
            rendered_dir=rendered,
            receipt_path=receipt,
            terraform_dir=terraform,
            plan_path=plan,
            plan_json_path=plan_json,
            runner=runner,
        )
        == 0
    )

    assert transient_paths
    assert all(not path.exists() for path in transient_paths)
    plan_commands = [call for call in calls if "plan" in call]
    assert plan_commands
    assert any(argument.startswith("-var-file=") for argument in plan_commands[0])
    assert not any(
        "runtime_boundary_policy_path" in argument
        for call in calls
        for argument in call
    )

    document = json.loads(plan_json.read_text(encoding="utf-8"))
    change = next(
        item
        for item in document["resource_changes"]
        if item["address"] == "terraform_data.account_guard"
    )
    assert change["change"]["after"]["input"] == POLICY

    shown = subprocess.run(
        [TERRAFORM, f"-chdir={terraform}", "show", "-json", str(plan)],
        env=_env(),
        capture_output=True,
        text=True,
    )
    assert shown.returncode == 0, shown.stdout + shown.stderr
    _add_plan_receipt(receipt, plan, shown.stdout)

    assert (
        runtime_exec.run_apply(
            plan_path=plan,
            manifest_path=manifest,
            rendered_dir=rendered,
            receipt_path=receipt,
            terraform_dir=terraform,
            runner=runner,
        )
        == 0
    )

    output = _tf("output", "-json", "policy", cwd=terraform)
    assert output.returncode == 0, output.stdout + output.stderr
    assert json.loads(output.stdout) == POLICY


def test_terraform_module_uses_policy_value_not_file_path():
    """Guard the reviewed module against reintroducing the transient path input."""
    main = (REPO / "infra/terraform/main.tf").read_text(encoding="utf-8")
    variables = (REPO / "infra/terraform/variables.tf").read_text(encoding="utf-8")
    versions = (REPO / "infra/terraform/versions.tf").read_text(encoding="utf-8")

    assert "runtime_boundary_policy_json" in main
    assert 'variable "runtime_boundary_policy_json"' in variables
    for text in (main, variables, versions):
        assert "runtime_boundary_policy_path" not in text
    assert "templatefile(local.runtime_boundary_policy_path" not in main
