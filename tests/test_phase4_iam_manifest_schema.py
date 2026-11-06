"""Schema and byte-integrity tests for the shared IAM manifest verifier."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import phase4_iam_manifest as builder  # noqa: E402
import phase4_iam_manifest_schema as schema  # noqa: E402


def _fixture(tmp_path: Path):
    rendered = tmp_path / "iam"
    rendered.mkdir()
    for name in builder.ACTIVE:
        (rendered / name).write_bytes(b'{"ok":true}')
    main = tmp_path / "main.tf"
    main.write_bytes(b"source")
    document = builder.build_manifest(rendered, main)
    return rendered, main, document


def test_builder_output_validates_and_verifies(tmp_path):
    rendered, _, document = _fixture(tmp_path)
    schema.validate_manifest(document)
    loaded = schema.verified_documents(
        document, rendered, ("bootstrap-trust-policy.json",)
    )
    assert loaded["bootstrap-trust-policy.json"] == b'{"ok":true}'


def test_malformed_manifest_structures_are_rejected(tmp_path):
    _, _, document = _fixture(tmp_path)
    for mutate in (
        lambda d: d.update(schema="wrong"),
        lambda d: d["active_documents"].pop("bootstrap-policy.json"),
        lambda d: d["active_documents"]["bootstrap-policy.json"].update(sha256="bad"),
        lambda d: d["active_documents"]["bootstrap-policy.json"].update(bytes=0),
        lambda d: d.update(active_document_count=99),
    ):
        candidate = copy.deepcopy(document)
        mutate(candidate)
        with pytest.raises(schema.ManifestError):
            schema.validate_manifest(candidate)


def test_size_and_hash_mismatch_are_rejected(tmp_path):
    rendered, _, document = _fixture(tmp_path)
    (rendered / "bootstrap-policy.json").write_bytes(b'{"ok":true,"extra":1}')
    with pytest.raises(schema.ManifestError):
        schema.verified_documents(document, rendered, ("bootstrap-policy.json",))


def test_terraform_source_hash_mismatch_is_rejected(tmp_path):
    _, main, document = _fixture(tmp_path)
    main.write_bytes(b"changed")
    with pytest.raises(schema.ManifestError):
        schema.verify_terraform_source(document, main)


def test_builder_carries_validation_for_unchanged_active_documents(tmp_path):
    rendered, main, document = _fixture(tmp_path)
    previous = tmp_path / "previous.json"
    document["validation"] = {"status": "passed", "findings": 0}
    previous.write_text(json.dumps(document), encoding="utf-8")

    rebuilt = builder.build_manifest(rendered, main, previous)

    assert rebuilt["validation"] == {"status": "passed", "findings": 0}


def test_builder_drops_validation_when_active_documents_change(tmp_path):
    rendered, main, document = _fixture(tmp_path)
    previous = tmp_path / "previous.json"
    document["validation"] = {"status": "passed", "findings": 0}
    previous.write_text(json.dumps(document), encoding="utf-8")
    (rendered / "runtime-boundary-policy.json").write_bytes(b'{"changed":true}')

    rebuilt = builder.build_manifest(rendered, main, previous)

    assert "validation" not in rebuilt
