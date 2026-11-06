"""Shared schema and byte verifier for the active Phase 4 IAM manifest."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

SCHEMA = "phase4-active-iam-manifest-v2"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
ENTRY_KEYS = {"sha256", "bytes", "applied_by"}
EXCLUDED_KEYS = {"sha256", "reason"}


class ManifestError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ManifestError(message)


def validate_manifest(document: dict) -> dict:
    _require(isinstance(document, dict), "manifest must be an object")
    _require(document.get("schema") == SCHEMA, "unexpected manifest schema")
    active = document.get("active_documents")
    _require(isinstance(active, dict) and active, "active_documents must be a non-empty object")
    for name, entry in active.items():
        _require(isinstance(name, str) and name.endswith(".json"), f"invalid active document name: {name}")
        _require(isinstance(entry, dict), f"active entry must be an object: {name}")
        _require(set(entry) == ENTRY_KEYS, f"unexpected active-entry keys: {name}")
        _require(bool(HEX64.match(str(entry.get("sha256", "")))), f"invalid sha256: {name}")
        _require(isinstance(entry.get("bytes"), int) and entry["bytes"] > 0, f"invalid byte size: {name}")
        _require(
            isinstance(entry.get("applied_by"), str) and bool(entry["applied_by"]),
            f"invalid owner: {name}",
        )
    _require(document.get("active_document_count") == len(active), "active document count mismatch")
    excluded = document.get("excluded_optional_oidc")
    _require(isinstance(excluded, dict), "excluded_optional_oidc must be an object")
    for name, entry in excluded.items():
        _require(isinstance(entry, dict), f"excluded entry must be an object: {name}")
        _require(set(entry) == EXCLUDED_KEYS, f"unexpected excluded-entry keys: {name}")
        _require(bool(HEX64.match(str(entry.get("sha256", "")))), f"invalid excluded sha256: {name}")
        _require(isinstance(entry.get("reason"), str) and bool(entry["reason"]), f"invalid reason: {name}")
    _require(document.get("excluded_optional_oidc_count") == len(excluded), "excluded count mismatch")
    source = document.get("terraform_generated_source")
    _require(isinstance(source, dict), "terraform_generated_source must be an object")
    _require(bool(HEX64.match(str(source.get("sha256", "")))), "invalid Terraform source sha256")
    _require(isinstance(source.get("path"), str) and bool(source["path"]), "invalid Terraform source path")
    _require(isinstance(source.get("covers"), list) and bool(source["covers"]), "invalid Terraform source covers")
    validation = document.get("validation")
    if validation is not None:
        _require(isinstance(validation, dict), "validation must be an object")
    return document


def load_manifest(path: Path) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"manifest cannot be read: {path}") from exc
    return validate_manifest(document)


def verified_documents(
    manifest: dict,
    rendered_dir: Path,
    required_names: tuple[str, ...],
) -> dict[str, bytes]:
    """Return exact verified bytes for required names; reject any mismatch."""
    active = manifest["active_documents"]
    loaded: dict[str, bytes] = {}
    for name in required_names:
        _require(name in active, f"required document is not active: {name}")
        path = rendered_dir / name
        _require(path.is_file(), f"required rendered document is missing: {name}")
        data = path.read_bytes()
        entry = active[name]
        _require(len(data) == entry["bytes"], f"reviewed byte size mismatch: {name}")
        _require(hashlib.sha256(data).hexdigest() == entry["sha256"], f"reviewed hash mismatch: {name}")
        loaded[name] = data
    return loaded


def verify_terraform_source(manifest: dict, source_path: Path) -> bytes:
    _require(source_path.is_file(), "reviewed Terraform source is missing")
    data = source_path.read_bytes()
    expected = manifest["terraform_generated_source"]["sha256"]
    _require(hashlib.sha256(data).hexdigest() == expected, "reviewed Terraform source hash mismatch")
    return data
