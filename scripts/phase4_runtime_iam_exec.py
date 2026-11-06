"""Fixed wrapper for the runtime IAM saved-plan/apply path.

Verified runtime boundary bytes are passed to Terraform as a plan-time input
value, not as a file path. The private var file is transient and is removed after
planning; the saved plan contains the value and does not need the file at apply.
The Terraform source hash is checked before planning. The plan JSON is scope
checked, hashed, and must match before apply. No arbitrary Terraform command is
accepted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from phase4_iam_manifest_schema import verified_documents, verify_terraform_source
from phase4_review_receipt import verify_accepted_inputs, verify_reviewed_plan
from phase4_iam_plan_scope import validate_plan

REPO = Path(__file__).resolve().parents[1]
RENDERED_DIR = REPO / "var" / "phase4" / "iam"
MANIFEST_FILE = RENDERED_DIR / "active-manifest.json"
RECEIPT_FILE = REPO / "var" / "phase4" / "independent-review-receipt.json"
TERRAFORM_DIR = REPO / "infra" / "terraform"
BOUNDARY_DOC = "runtime-boundary-policy.json"
TERRAFORM_MAIN = TERRAFORM_DIR / "main.tf"
DEFAULT_PLAN = TERRAFORM_DIR / "phase4-runtime-iam.tfplan"
DEFAULT_PLAN_JSON = TERRAFORM_DIR / "phase4-runtime-iam.json"


def _secure_policy_var_file(policy_json: bytes) -> Path:
    """Write the verified policy as a transient Terraform input value.

    Terraform records the value in the saved plan. The file is only needed while
    planning and is deleted immediately afterwards; apply must not depend on it.
    """
    payload = json.dumps(
        {"runtime_boundary_policy_json": policy_json.decode("utf-8")}
    )
    fd, name = tempfile.mkstemp(prefix="phase4-policy-", suffix=".tfvars.json")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
    try:
        os.chmod(name, 0o600)
    except OSError:
        pass
    return Path(name)


def _plan_targets() -> list[str]:
    return [
        "-target=terraform_data.account_guard",
        "-target=aws_iam_policy.runtime_boundary",
        "-target=aws_iam_role.instance",
        "-target=aws_iam_role_policy.logs",
        "-target=aws_iam_role_policy.ecr_pull",
        "-target=aws_iam_role_policy_attachment.ssm",
        "-target=aws_iam_instance_profile.app",
    ]


def run_plan(
    *,
    manifest_path: Path = MANIFEST_FILE,
    rendered_dir: Path = RENDERED_DIR,
    receipt_path: Path = RECEIPT_FILE,
    terraform_dir: Path = TERRAFORM_DIR,
    plan_path: Path = DEFAULT_PLAN,
    plan_json_path: Path = DEFAULT_PLAN_JSON,
    extra_args: list[str] | None = None,
    runner=subprocess.run,
) -> int:
    _, manifest = verify_accepted_inputs(
        receipt_path=receipt_path,
        manifest_path=manifest_path,
        rendered_dir=rendered_dir,
    )
    boundary = verified_documents(manifest, rendered_dir, (BOUNDARY_DOC,))[BOUNDARY_DOC]
    verify_terraform_source(manifest, terraform_dir / "main.tf")

    temp_path = _secure_policy_var_file(boundary)
    try:
        command = [
            "terraform",
            f"-chdir={terraform_dir}",
            "plan",
            *_plan_targets(),
            "-input=false",
            f"-var-file={temp_path}",
            f"-out={plan_path}",
            *(extra_args or []),
        ]
        completed = runner(command)
        if getattr(completed, "returncode", 1) != 0:
            return getattr(completed, "returncode", 1)

        show = runner(
            ["terraform", f"-chdir={terraform_dir}", "show", "-json", str(plan_path)],
            capture_output=True,
            text=True,
        )
        if getattr(show, "returncode", 1) != 0:
            return getattr(show, "returncode", 1)
        document = json.loads(getattr(show, "stdout", "") or "{}")
        errors = validate_plan(document)
        if errors:
            for error in errors:
                print(error, file=sys.stderr)
            return 1
        plan_json_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        plan_hash = hashlib.sha256(plan_path.read_bytes()).hexdigest()
        plan_path.with_suffix(plan_path.suffix + ".sha256").write_text(
            plan_hash + "\n", encoding="utf-8"
        )
        print(f"runtime_iam_plan_ready resources={len(document.get('resource_changes', []))}")
        return 0
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass


def run_apply(
    *,
    plan_path: Path = DEFAULT_PLAN,
    manifest_path: Path = MANIFEST_FILE,
    rendered_dir: Path = RENDERED_DIR,
    receipt_path: Path = RECEIPT_FILE,
    terraform_dir: Path = TERRAFORM_DIR,
    runner=subprocess.run,
) -> int:
    receipt, _ = verify_accepted_inputs(
        receipt_path=receipt_path,
        manifest_path=manifest_path,
        rendered_dir=rendered_dir,
        require_plan=True,
    )
    if not plan_path.is_file():
        raise RuntimeError("reviewed saved plan binary is missing")
    reviewed_plan_hash = receipt["runtime_iam_plan"]["plan_sha256"]
    if hashlib.sha256(plan_path.read_bytes()).hexdigest() != reviewed_plan_hash:
        raise RuntimeError("saved plan binary changed since independent review")
    show = runner(
        ["terraform", f"-chdir={terraform_dir}", "show", "-json", str(plan_path)],
        capture_output=True,
        text=True,
    )
    if getattr(show, "returncode", 1) != 0:
        return getattr(show, "returncode", 1)
    raw_json = getattr(show, "stdout", "").encode("utf-8")
    document = json.loads(raw_json.decode("utf-8"))
    errors = validate_plan(document)
    if errors:
        raise RuntimeError("derived saved-plan scope check failed: " + "; ".join(errors))
    verify_reviewed_plan(receipt, plan_path, raw_json)
    completed = runner(
        ["terraform", f"-chdir={terraform_dir}", "apply", str(plan_path)]
    )
    return getattr(completed, "returncode", 1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--manifest", type=Path, default=MANIFEST_FILE)
    parser.add_argument("--receipt", type=Path, default=RECEIPT_FILE)
    parser.add_argument("--rendered-dir", type=Path, default=RENDERED_DIR)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--plan-json", type=Path, default=DEFAULT_PLAN_JSON)
    parser.add_argument("--var-file", action="append", default=[])
    args = parser.parse_args()
    try:
        if args.action == "plan":
            extra = []
            for var_file in args.var_file:
                extra.extend(["-var-file", var_file])
            return run_plan(
                manifest_path=args.manifest,
                rendered_dir=args.rendered_dir,
                receipt_path=args.receipt,
                plan_path=args.plan,
                plan_json_path=args.plan_json,
                extra_args=extra,
            )
        return run_apply(
            plan_path=args.plan,
            manifest_path=args.manifest,
            rendered_dir=args.rendered_dir,
            receipt_path=args.receipt,
        )
    except Exception as exc:
        print(f"runtime_iam_action_failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
