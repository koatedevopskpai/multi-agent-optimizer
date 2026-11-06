"""Codex-owned independent-review receipt for the active IAM bundle/plan.

Proposal generation must never create this receipt. Execution loads the receipt,
verifies the accepted manifest digest/source hash, and optionally the reviewed
saved-plan binary/JSON hashes before any MFA or AWS activity.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from phase4_iam_manifest_schema import load_manifest

SCHEMA = "phase4-independent-review-receipt-v1"
SCOPE = "phase4-iam-active-named-profile"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
REQUIRED = {
    "schema",
    "reviewer",
    "reviewed_at_utc",
    "decision",
    "scope",
    "manifest_schema",
    "manifest_sha256",
    "active_document_count",
    "terraform_source_sha256",
}


class ReceiptError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReceiptError(message)


def validate_receipt(document: dict) -> dict:
    _require(isinstance(document, dict), "receipt must be an object")
    _require(REQUIRED <= set(document), "receipt is missing required fields")
    _require(set(document) <= REQUIRED | {"runtime_iam_plan"}, "receipt has unexpected fields")
    _require(document.get("schema") == SCHEMA, "unexpected receipt schema")
    _require(document.get("decision") == "accepted", "receipt decision is not accepted")
    _require(document.get("scope") == SCOPE, "receipt scope mismatch")
    _require(isinstance(document.get("reviewer"), str) and document["reviewer"], "receipt reviewer missing")
    _require(
        isinstance(document.get("reviewed_at_utc"), str) and document["reviewed_at_utc"],
        "receipt review timestamp missing",
    )
    _require(
        isinstance(document.get("manifest_sha256"), str)
        and bool(HEX64.match(document["manifest_sha256"])),
        "receipt manifest digest invalid",
    )
    _require(
        isinstance(document.get("terraform_source_sha256"), str)
        and bool(HEX64.match(document["terraform_source_sha256"])),
        "receipt Terraform source digest invalid",
    )
    plan = document.get("runtime_iam_plan")
    if plan is not None:
        _require(isinstance(plan, dict), "runtime_iam_plan must be an object")
        _require(
            set(plan) == {"reviewed_at_utc", "plan_sha256", "plan_json_sha256"},
            "runtime_iam_plan fields invalid",
        )
        _require(
            isinstance(plan.get("reviewed_at_utc"), str) and plan["reviewed_at_utc"],
            "plan review timestamp missing",
        )
        for key in ("plan_sha256", "plan_json_sha256"):
            _require(
                isinstance(plan.get(key), str) and bool(HEX64.match(plan[key])),
                f"plan digest invalid: {key}",
            )
    return document


def load_receipt(path: Path) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"review receipt cannot be read: {path}") from exc
    return validate_receipt(document)


def verify_accepted_inputs(
    *,
    receipt_path: Path,
    manifest_path: Path,
    rendered_dir: Path,
    require_plan: bool = False,
) -> tuple[dict, dict]:
    receipt = load_receipt(receipt_path)
    manifest = load_manifest(manifest_path)
    manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    _require(
        manifest_digest == receipt["manifest_sha256"],
        "manifest changed since independent review",
    )
    _require(
        receipt["manifest_schema"] == manifest["schema"],
        "manifest schema differs from reviewed receipt",
    )
    _require(
        receipt["active_document_count"] == manifest["active_document_count"],
        "active document count differs from reviewed receipt",
    )
    _require(
        receipt["terraform_source_sha256"]
        == manifest["terraform_generated_source"]["sha256"],
        "Terraform source differs from reviewed receipt",
    )
    if require_plan:
        _require("runtime_iam_plan" in receipt, "independent saved-plan review receipt is missing")
    return receipt, manifest


def verify_reviewed_plan(receipt: dict, plan_path: Path, plan_json_bytes: bytes) -> None:
    plan = receipt.get("runtime_iam_plan")
    _require(isinstance(plan, dict), "independent saved-plan review receipt is missing")
    _require(plan_path.is_file(), "saved plan binary is missing")
    actual_plan = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    _require(actual_plan == plan["plan_sha256"], "saved plan binary changed since independent review")
    actual_json = hashlib.sha256(plan_json_bytes).hexdigest()
    _require(actual_json == plan["plan_json_sha256"], "derived plan JSON differs from independent review")
