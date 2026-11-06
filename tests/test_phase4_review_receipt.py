"""Tests for the Codex-owned independent IAM review receipt contract."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import phase4_iam_manifest as builder  # noqa: E402
import phase4_review_receipt as receipt_module  # noqa: E402


def _manifest_fixture(tmp_path: Path):
    rendered = tmp_path / "iam"
    rendered.mkdir()
    for name in builder.ACTIVE:
        (rendered / name).write_bytes(b'{"Version":"2012-10-17","Statement":[]}')
    main = tmp_path / "main.tf"
    main.write_bytes(b"source")
    document = builder.build_manifest(rendered, main)
    manifest_path = rendered / "active-manifest.json"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    return rendered, main, manifest_path, document


def _receipt(manifest_path: Path, document: dict, *, plan: dict | None = None) -> dict:
    value = {
        "schema": receipt_module.SCHEMA,
        "reviewer": "synthetic-test-codex",
        "reviewed_at_utc": "2026-11-04T00:00:00Z",
        "decision": "accepted",
        "scope": receipt_module.SCOPE,
        "manifest_schema": document["schema"],
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "active_document_count": document["active_document_count"],
        "terraform_source_sha256": document["terraform_generated_source"]["sha256"],
    }
    if plan is not None:
        value["runtime_iam_plan"] = plan
    return value


def test_valid_synthetic_receipt_accepts_manifest(tmp_path):
    rendered, _, manifest_path, document = _manifest_fixture(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(_receipt(manifest_path, document)), encoding="utf-8")
    receipt, manifest = receipt_module.verify_accepted_inputs(
        receipt_path=receipt_path, manifest_path=manifest_path, rendered_dir=rendered
    )
    assert receipt["reviewer"] == "synthetic-test-codex"
    assert manifest["active_document_count"] == 9


def test_missing_or_malformed_receipt_is_rejected(tmp_path):
    rendered, _, manifest_path, _ = _manifest_fixture(tmp_path)
    with pytest.raises(receipt_module.ReceiptError):
        receipt_module.verify_accepted_inputs(
            receipt_path=tmp_path / "missing.json",
            manifest_path=manifest_path,
            rendered_dir=rendered,
        )


def test_changed_manifest_or_scope_is_rejected(tmp_path):
    rendered, _, manifest_path, document = _manifest_fixture(tmp_path)
    receipt = _receipt(manifest_path, document)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    # Change manifest after receipt; verification must fail.
    document["notes"].append("changed")
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(receipt_module.ReceiptError):
        receipt_module.verify_accepted_inputs(
            receipt_path=receipt_path, manifest_path=manifest_path, rendered_dir=rendered
        )
    # Wrong decision/scope also fails.
    bad = copy.deepcopy(receipt)
    bad["decision"] = "pending"
    with pytest.raises(receipt_module.ReceiptError):
        receipt_module.validate_receipt(bad)
    bad = copy.deepcopy(receipt)
    bad["scope"] = "other"
    with pytest.raises(receipt_module.ReceiptError):
        receipt_module.validate_receipt(bad)


def test_plan_receipt_is_required_and_hash_checked(tmp_path):
    rendered, _, manifest_path, document = _manifest_fixture(tmp_path)
    receipt = _receipt(manifest_path, document)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(receipt_module.ReceiptError):
        receipt_module.verify_accepted_inputs(
            receipt_path=receipt_path,
            manifest_path=manifest_path,
            rendered_dir=rendered,
            require_plan=True,
        )
    plan_path = tmp_path / "plan.tfplan"
    plan_path.write_bytes(b"plan")
    plan_json = b'{"resource_changes":[]}'
    plan_receipt = {
        "reviewed_at_utc": "2026-11-04T00:00:00Z",
        "plan_sha256": hashlib.sha256(b"plan").hexdigest(),
        "plan_json_sha256": hashlib.sha256(plan_json).hexdigest(),
    }
    receipt["runtime_iam_plan"] = plan_receipt
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    _, _ = receipt_module.verify_accepted_inputs(
        receipt_path=receipt_path,
        manifest_path=manifest_path,
        rendered_dir=rendered,
        require_plan=True,
    )
    receipt_module.verify_reviewed_plan(receipt, plan_path, plan_json)
    plan_path.write_bytes(b"replacement")
    with pytest.raises(receipt_module.ReceiptError):
        receipt_module.verify_reviewed_plan(receipt, plan_path, plan_json)


def test_manifest_builder_does_not_create_receipt(tmp_path):
    rendered, main, _, _ = _manifest_fixture(tmp_path)
    builder.build_manifest(rendered, main)
    assert not (tmp_path / "independent-review-receipt.json").exists()
