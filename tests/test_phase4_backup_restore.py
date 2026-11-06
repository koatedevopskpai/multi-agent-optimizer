"""Phase 4 local backup and restore behaviour."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

from delivery_recovery.storage.backup import (
    BackupError,
    create_backup,
    export_backup,
    import_backup,
    normalize_legacy_archive,
    restore_backup,
)
from delivery_recovery.storage.store import ApplicationStore
from delivery_recovery.storage.config import StorageConfig

from phase3_support import build_test_runtime, draft


def _runtime_with_runs(tmp_path: Path):
    runtime = build_test_runtime(tmp_path)
    value = draft()
    runtime.store.create_run(
        draft_json=value.model_dump(mode="json"),
        request_hash="backup-run-hash",
        run_id="run-backup",
        thread_id="run-backup",
        principal_id="planner.demo",
        idempotency_key="backup-create",
    )
    assert runtime.worker.run_once()["status"] == "awaiting_approval"
    return runtime


def test_backup_and_restore_preserve_runs_revisions_and_audit(tmp_path):
    runtime = _runtime_with_runs(tmp_path)
    try:
        run = runtime.store.get_run("run-backup")
        assert run["status"] == "awaiting_approval"
        binding = run["pending_decision"]
        manager = next(
            identity.principal
            for identity in runtime.identity.identities
            if identity.principal.is_manager()
        )
        from delivery_recovery.approvals import DecisionRequest

        runtime.decisions.submit(
            "run-backup",
            DecisionRequest(
                candidate_id=binding["candidate_id"],
                project_version=binding["project_version"],
                input_hash=binding["input_hash"],
                catalogue_hash=binding["catalogue_hash"],
                approved=True,
            ),
            principal=manager,
            idempotency_key="backup-approve",
        )
        assert runtime.worker.run_once()["status"] == "completed"
        before = runtime.store.get_run("run-backup")
        before_audit = runtime.store.list_audit("run-backup")
        before_revisions = runtime.store.list_revisions("run-backup")
    finally:
        runtime.close()

    backup_dir = tmp_path / "backup"
    manifest = create_backup(tmp_path, backup_dir)
    assert manifest["run_count"] == 1
    assert manifest["runs"][0]["run_id"] == "run-backup"
    assert manifest["runs"][0]["status"] == "completed"
    assert manifest["revision_count"] == 1
    assert manifest["audit_event_count"] == len(before_audit)
    assert "demo-identities.json" not in manifest["files"]

    restored = tmp_path / "restored"
    restore_manifest = restore_backup(backup_dir, restored)
    assert restore_manifest["integrity"] == "ok"

    config = StorageConfig(
        data_dir=restored,
        db_path=restored / "application.db",
        checkpoint_path=restored / "checkpoints.db",
    )
    store = ApplicationStore(config)
    restored_run = store.get_run("run-backup")
    assert restored_run is not None
    assert restored_run["status"] == before["status"] == "completed"
    assert restored_run["revision"] == before["revision"] == 1
    assert restored_run["decision_status"] == "approved"
    assert store.list_revisions("run-backup") == before_revisions
    assert store.list_audit("run-backup") == before_audit


def test_backup_rejects_missing_storage(tmp_path):
    with pytest.raises(BackupError) as error:
        create_backup(tmp_path / "missing", tmp_path / "backup")
    assert error.value.code == "BACKUP_SOURCE_INVALID"


def test_backup_checks_hashes_before_restore(tmp_path):
    runtime = _runtime_with_runs(tmp_path)
    try:
        pass
    finally:
        runtime.close()
    backup_dir = tmp_path / "backup"
    create_backup(tmp_path, backup_dir)
    (backup_dir / "application.db").write_bytes(b"tampered")
    with pytest.raises(BackupError) as error:
        restore_backup(backup_dir, tmp_path / "restored")
    assert error.value.code == "BACKUP_HASH_MISMATCH"


def test_backup_integrity_check_finds_corrupt_database(tmp_path):
    runtime = _runtime_with_runs(tmp_path)
    runtime.close()
    backup_dir = tmp_path / "backup"
    create_backup(tmp_path, backup_dir)
    corrupt = backup_dir / "application.db"
    corrupt.write_bytes(b"not a sqlite database")
    manifest_path = backup_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    import hashlib

    manifest["files"]["application.db"] = hashlib.sha256(
        corrupt.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BackupError):
        restore_backup(backup_dir, tmp_path / "restored")


def test_backup_can_include_identity_only_when_requested(tmp_path):
    runtime = _runtime_with_runs(tmp_path)
    runtime.close()
    identity = tmp_path / "demo-identities.json"
    identity.write_text("[]", encoding="utf-8")
    excluded = create_backup(tmp_path, tmp_path / "excluded")
    assert "demo-identities.json" not in excluded["files"]
    included = create_backup(
        tmp_path, tmp_path / "included", include_identity=True
    )
    assert "demo-identities.json" in included["files"]
    assert (tmp_path / "included" / "demo-identities.json").exists()


def test_export_import_and_fresh_restore_round_trip(tmp_path):
    runtime = _runtime_with_runs(tmp_path)
    runtime.close()
    backup_dir = tmp_path / "backup"
    create_backup(tmp_path, backup_dir, include_identity=True)
    archive = tmp_path / "transfer" / "recovery.tgz"
    exported = export_backup(backup_dir, archive)
    assert archive.is_file()
    assert exported["sha256"]

    imported_dir = tmp_path / "imported"
    imported = import_backup(archive, imported_dir)
    assert "application.db" in imported["files"]

    restored_dir = tmp_path / "restored"
    restored = restore_backup(imported_dir, restored_dir)
    assert restored["integrity"] == "ok"
    assert restored["run_count"] == 1
    assert restored["runs"][0]["status"] == "awaiting_approval"


def test_import_rejects_path_traversal(tmp_path):
    import io
    import tarfile

    archive = tmp_path / "unsafe.tgz"
    with tarfile.open(archive, "w:gz") as handle:
        info = tarfile.TarInfo("../escape")
        info.size = 1
        handle.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(BackupError) as error:
        import_backup(archive, tmp_path / "target")
    assert error.value.code == "BACKUP_ARCHIVE_UNSAFE"



def _runtime_completed(tmp_path: Path):
    runtime = _runtime_with_runs(tmp_path)
    run = runtime.store.get_run("run-backup")
    binding = run["pending_decision"]
    manager = next(
        identity.principal
        for identity in runtime.identity.identities
        if identity.principal.is_manager()
    )
    from delivery_recovery.approvals import DecisionRequest

    runtime.decisions.submit(
        "run-backup",
        DecisionRequest(
            candidate_id=binding["candidate_id"],
            project_version=binding["project_version"],
            input_hash=binding["input_hash"],
            catalogue_hash=binding["catalogue_hash"],
            approved=True,
        ),
        principal=manager,
        idempotency_key="backup-approve",
    )
    assert runtime.worker.run_once()["status"] == "completed"
    return runtime


def _rehash_backup(backup_dir: Path, name: str, manifest: dict) -> None:
    import hashlib

    manifest["files"][name] = hashlib.sha256(
        (backup_dir / name).read_bytes()
    ).hexdigest()
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


def test_backup_rejects_corrupt_source_with_backup_error(tmp_path):
    runtime = _runtime_with_runs(tmp_path)
    runtime.close()
    database = tmp_path / "application.db"
    page_size = sqlite3.connect(database).execute("PRAGMA page_size").fetchone()[0]
    data = bytearray(database.read_bytes())
    data[page_size : page_size * 2] = b"\xff" * page_size
    database.write_bytes(data)

    with pytest.raises(BackupError) as error:
        create_backup(tmp_path, tmp_path / "backup")
    assert error.value.code in {"BACKUP_FAILED", "BACKUP_INTEGRITY_FAILED"}


def test_restore_rejects_missing_required_checkpoints(tmp_path):
    runtime = _runtime_with_runs(tmp_path)
    runtime.close()
    backup_dir = tmp_path / "backup"
    create_backup(tmp_path, backup_dir)
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    del manifest["files"]["checkpoints.db"]
    (backup_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (backup_dir / "checkpoints.db").unlink()
    import hashlib

    source_before = hashlib.sha256(
        (tmp_path / "application.db").read_bytes()
    ).hexdigest()
    backup_before = hashlib.sha256(
        (backup_dir / "application.db").read_bytes()
    ).hexdigest()

    with pytest.raises(BackupError) as error:
        restore_backup(backup_dir, tmp_path / "restored")
    assert error.value.code == "BACKUP_MANIFEST_INVALID"
    assert hashlib.sha256(
        (tmp_path / "application.db").read_bytes()
    ).hexdigest() == source_before
    assert hashlib.sha256(
        (backup_dir / "application.db").read_bytes()
    ).hexdigest() == backup_before


def test_restore_rejects_revision_count_mismatch(tmp_path):
    runtime = _runtime_completed(tmp_path)
    runtime.close()
    backup_dir = tmp_path / "backup"
    create_backup(tmp_path, backup_dir)
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    connection = sqlite3.connect(backup_dir / "application.db")
    connection.execute("DELETE FROM revisions")
    connection.commit()
    connection.close()
    _rehash_backup(backup_dir, "application.db", manifest)

    with pytest.raises(BackupError) as error:
        restore_backup(backup_dir, tmp_path / "restored")
    assert error.value.code == "BACKUP_INVENTORY_MISMATCH"


def test_restore_rejects_audit_count_mismatch(tmp_path):
    runtime = _runtime_with_runs(tmp_path)
    runtime.close()
    backup_dir = tmp_path / "backup"
    create_backup(tmp_path, backup_dir)
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    connection = sqlite3.connect(backup_dir / "application.db")
    connection.execute("DELETE FROM audit_events")
    connection.commit()
    connection.close()
    _rehash_backup(backup_dir, "application.db", manifest)

    with pytest.raises(BackupError) as error:
        restore_backup(backup_dir, tmp_path / "restored")
    assert error.value.code == "BACKUP_INVENTORY_MISMATCH"


def test_restore_rejects_run_inventory_mismatch(tmp_path):
    runtime = _runtime_with_runs(tmp_path)
    runtime.close()
    backup_dir = tmp_path / "backup"
    create_backup(tmp_path, backup_dir)
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    connection = sqlite3.connect(backup_dir / "application.db")
    connection.execute("UPDATE runs SET status='failed', revision=0")
    connection.commit()
    connection.close()
    _rehash_backup(backup_dir, "application.db", manifest)

    with pytest.raises(BackupError) as error:
        restore_backup(backup_dir, tmp_path / "restored")
    assert error.value.code == "BACKUP_INVENTORY_MISMATCH"


def test_import_rejects_manifest_without_required_databases(tmp_path):
    runtime = _runtime_with_runs(tmp_path)
    runtime.close()
    backup_dir = tmp_path / "backup"
    create_backup(tmp_path, backup_dir)
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    del manifest["files"]["checkpoints.db"]
    (backup_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    modified_manifest = tmp_path / "modified-manifest.json"
    modified_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    import tarfile

    archive = tmp_path / "transfer" / "bad.tgz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(backup_dir / "application.db", arcname="application.db")
        handle.add(backup_dir / "checkpoints.db", arcname="checkpoints.db")
        handle.add(modified_manifest, arcname="manifest.json")

    with pytest.raises(BackupError) as error:
        import_backup(archive, tmp_path / "imported")
    assert error.value.code == "BACKUP_MANIFEST_INVALID"


def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "phase4_backup_restore.py"), *args],
        capture_output=True,
        text=True,
    )


def test_export_archives_only_manifest_listed_files_and_manifest(tmp_path):
    import tarfile

    runtime = _runtime_with_runs(tmp_path)
    runtime.close()
    backup_dir = tmp_path / "backup"
    manifest = create_backup(tmp_path, backup_dir)
    (backup_dir / "unlisted-secret.txt").write_text("must not ship", encoding="utf-8")
    archive = tmp_path / "transfer" / "recovery.tgz"
    exported = export_backup(backup_dir, archive)
    with tarfile.open(archive, "r:gz") as handle:
        names = set(handle.getnames())
    assert names == {"manifest.json", *manifest["files"]}
    assert "unlisted-secret.txt" not in names
    assert exported["files"] == sorted(manifest["files"])


@pytest.mark.parametrize("value", ["[]", "null", '"scalar"', "123"])
def test_cli_restore_rejects_non_object_manifest(tmp_path, value):
    runtime = _runtime_with_runs(tmp_path)
    runtime.close()
    backup_dir = tmp_path / "backup"
    create_backup(tmp_path, backup_dir)
    (backup_dir / "manifest.json").write_text(value, encoding="utf-8")
    target = tmp_path / "restored"
    completed = _cli("restore", "--backup", str(backup_dir), "--target", str(target))
    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert payload["error"]["code"] == "BACKUP_MANIFEST_INVALID"
    assert "Traceback" not in completed.stderr
    assert not (target / "restore-manifest.json").exists()


@pytest.mark.parametrize("value", ["[]", "null", '"scalar"', "123"])
def test_cli_export_rejects_non_object_manifest(tmp_path, value):
    runtime = _runtime_with_runs(tmp_path)
    runtime.close()
    backup_dir = tmp_path / "backup"
    create_backup(tmp_path, backup_dir)
    (backup_dir / "manifest.json").write_text(value, encoding="utf-8")
    archive = tmp_path / "transfer" / "bad.tgz"
    completed = _cli("export", "--backup", str(backup_dir), "--archive", str(archive))
    assert completed.returncode == 1
    assert json.loads(completed.stdout)["error"]["code"] == "BACKUP_MANIFEST_INVALID"
    assert not archive.exists()


def test_import_rejects_unexpected_archive_member(tmp_path):
    import io
    import tarfile

    runtime = _runtime_with_runs(tmp_path)
    runtime.close()
    backup_dir = tmp_path / "backup"
    create_backup(tmp_path, backup_dir)
    valid = tmp_path / "transfer" / "valid.tgz"
    export_backup(backup_dir, valid)
    bad = tmp_path / "transfer" / "bad.tgz"
    with tarfile.open(valid, "r:gz") as source, tarfile.open(bad, "w:gz") as target:
        for member in source.getmembers():
            target.addfile(member, source.extractfile(member))
        extra = tarfile.TarInfo("unexpected.txt")
        extra.size = 1
        target.addfile(extra, io.BytesIO(b"x"))
    with pytest.raises(BackupError) as error:
        import_backup(bad, tmp_path / "imported")
    assert error.value.code == "BACKUP_ARCHIVE_UNEXPECTED_MEMBER"
    assert not (tmp_path / "imported" / "unexpected.txt").exists()


def test_import_rejects_symlink_archive_member(tmp_path):
    import tarfile

    runtime = _runtime_with_runs(tmp_path)
    runtime.close()
    backup_dir = tmp_path / "backup"
    create_backup(tmp_path, backup_dir)
    valid = tmp_path / "transfer" / "valid.tgz"
    export_backup(backup_dir, valid)
    bad = tmp_path / "transfer" / "link.tgz"
    with tarfile.open(valid, "r:gz") as source, tarfile.open(bad, "w:gz") as target:
        for member in source.getmembers():
            target.addfile(member, source.extractfile(member))
        link = tarfile.TarInfo("escape-link")
        link.type = tarfile.SYMTYPE
        link.linkname = "application.db"
        target.addfile(link)
    with pytest.raises(BackupError) as error:
        import_backup(bad, tmp_path / "imported")
    assert error.value.code == "BACKUP_ARCHIVE_UNSAFE"
    assert not (tmp_path / "imported" / "escape-link").exists()


def test_import_rejects_non_object_manifest(tmp_path):
    import tarfile

    runtime = _runtime_with_runs(tmp_path)
    runtime.close()
    backup_dir = tmp_path / "backup"
    create_backup(tmp_path, backup_dir)
    valid = tmp_path / "transfer" / "valid.tgz"
    export_backup(backup_dir, valid)
    bad = tmp_path / "transfer" / "bad-manifest.tgz"
    with tarfile.open(valid, "r:gz") as source, tarfile.open(bad, "w:gz") as target:
        for member in source.getmembers():
            if member.name == "manifest.json":
                replacement = tarfile.TarInfo("manifest.json")
                replacement.size = 2
                target.addfile(replacement, __import__("io").BytesIO(b"[]"))
            else:
                target.addfile(member, source.extractfile(member))
    with pytest.raises(BackupError) as error:
        import_backup(bad, tmp_path / "imported")
    assert error.value.code == "BACKUP_MANIFEST_INVALID"


@pytest.mark.parametrize("value", ["[]", "null", '"scalar"', "123"])
def test_cli_import_rejects_non_object_manifest(tmp_path, value):
    import tarfile

    runtime = _runtime_with_runs(tmp_path)
    runtime.close()
    backup_dir = tmp_path / "backup"
    create_backup(tmp_path, backup_dir)
    valid = tmp_path / "transfer" / "valid.tgz"
    export_backup(backup_dir, valid)
    bad = tmp_path / "transfer" / "bad.tgz"
    payload = value.encode("utf-8")
    with tarfile.open(valid, "r:gz") as source, tarfile.open(bad, "w:gz") as target:
        for member in source.getmembers():
            if member.name == "manifest.json":
                replacement = tarfile.TarInfo("manifest.json")
                replacement.size = len(payload)
                import io

                target.addfile(replacement, io.BytesIO(payload))
            else:
                target.addfile(member, source.extractfile(member))
    target_dir = tmp_path / "imported"
    completed = _cli("import", "--archive", str(bad), "--target", str(target_dir))
    assert completed.returncode == 1
    assert json.loads(completed.stdout)["error"]["code"] == "BACKUP_MANIFEST_INVALID"
    assert "Traceback" not in completed.stderr


# --- Explicit legacy-archive compatibility (db4a072 exporter sidecars) ---

LEGACY_SIDECARS = (
    "application.db-shm",
    "application.db-wal",
    "checkpoints.db-shm",
    "checkpoints.db-wal",
)


def _copy_archive(
    source: Path,
    destination: Path,
    *,
    exclude: tuple[str, ...] = (),
    replace: dict[str, bytes] | None = None,
    extras: tuple[tuple[tarfile.TarInfo, bytes | None], ...] = (),
) -> None:
    replace = replace or {}
    with tarfile.open(source, "r:gz") as src, tarfile.open(destination, "w:gz") as dst:
        for member in src.getmembers():
            if member.name in exclude:
                continue
            if member.name in replace:
                payload = replace[member.name]
                info = tarfile.TarInfo(member.name)
                info.size = len(payload)
                dst.addfile(info, io.BytesIO(payload))
                continue
            dst.addfile(member, src.extractfile(member))
        for info, payload in extras:
            dst.addfile(info, None if payload is None else io.BytesIO(payload))


def _member(name: str, payload: bytes) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    return info, payload


def _clean_archive(tmp_path: Path) -> Path:
    runtime = _runtime_with_runs(tmp_path)
    runtime.close()
    backup_dir = tmp_path / "legacy-backup"
    create_backup(tmp_path, backup_dir, include_identity=True)
    archive = tmp_path / "clean.tgz"
    export_backup(backup_dir, archive)
    return archive


def _legacy_archive(
    tmp_path: Path,
    *,
    wal_payloads: dict[str, bytes] | None = None,
    extras: tuple[tuple[tarfile.TarInfo, bytes | None], ...] = (),
) -> Path:
    clean = _clean_archive(tmp_path)
    legacy = tmp_path / "legacy.tgz"
    sidecars = []
    for name in LEGACY_SIDECARS:
        payload = b"" if name.endswith("-wal") else b"\x00" * 32768
        if wal_payloads and name in wal_payloads:
            payload = wal_payloads[name]
        sidecars.append(_member(name, payload))
    _copy_archive(clean, legacy, extras=tuple(sidecars) + extras)
    return legacy


def test_legacy_normalization_is_explicit_and_preserves_original(tmp_path):
    legacy = _legacy_archive(tmp_path)
    original_bytes = legacy.read_bytes()
    original_hash = hashlib.sha256(original_bytes).hexdigest()

    # Ordinary strict import must keep rejecting the raw legacy archive.
    with pytest.raises(BackupError) as rejected:
        import_backup(legacy, tmp_path / "raw-import")
    assert rejected.value.code == "BACKUP_ARCHIVE_UNEXPECTED_MEMBER"

    derived = tmp_path / "derived.tgz"
    receipt = tmp_path / "legacy-normalization.json"
    result = normalize_legacy_archive(legacy, derived, receipt_path=receipt)

    assert legacy.read_bytes() == original_bytes
    assert result["original_sha256"] == original_hash
    assert result["normalized_sha256"] == hashlib.sha256(derived.read_bytes()).hexdigest()
    assert result["excluded_members"] == sorted(LEGACY_SIDECARS)
    assert result["excluded_wal_sizes_zero"] is True
    assert result["all_manifest_file_hashes_match"] is True
    assert result["original_archive_unchanged"] is True
    assert json.loads(receipt.read_text(encoding="utf-8"))["original_sha256"] == original_hash

    with tarfile.open(derived, "r:gz") as handle:
        names = set(handle.getnames())
    assert names == {
        "manifest.json",
        "application.db",
        "checkpoints.db",
        "demo-identities.json",
    }

    imported = import_backup(derived, tmp_path / "derived-import")
    assert "application.db" in imported["files"]
    restored = restore_backup(tmp_path / "derived-import", tmp_path / "derived-restore")
    assert restored["run_count"] == 1


def test_legacy_normalization_rejects_nonempty_wal(tmp_path):
    legacy = _legacy_archive(
        tmp_path, wal_payloads={"application.db-wal": b"committed-data"}
    )
    derived = tmp_path / "derived.tgz"
    with pytest.raises(BackupError) as error:
        normalize_legacy_archive(legacy, derived)
    assert error.value.code == "BACKUP_LEGACY_WAL_NONEMPTY"
    assert not derived.exists()


@pytest.mark.parametrize("extra_name", ["notes.txt", "application.db-journal", "application.db"])
def test_legacy_normalization_rejects_unknown_extras(tmp_path, extra_name):
    legacy = _legacy_archive(tmp_path, extras=(_member(extra_name, b"extra"),))
    with pytest.raises(BackupError) as error:
        normalize_legacy_archive(legacy, tmp_path / "derived.tgz")
    assert error.value.code in {
        "BACKUP_ARCHIVE_UNEXPECTED_MEMBER",
        "BACKUP_ARCHIVE_UNSAFE",
    }


def test_legacy_normalization_rejects_missing_required_member(tmp_path):
    clean = _clean_archive(tmp_path)
    legacy = tmp_path / "missing.tgz"
    _copy_archive(clean, legacy, exclude=("checkpoints.db",))
    with pytest.raises(BackupError) as error:
        normalize_legacy_archive(legacy, tmp_path / "derived.tgz")
    assert error.value.code == "BACKUP_ARCHIVE_MISSING_MEMBER"


def test_legacy_normalization_rejects_missing_manifest(tmp_path):
    clean = _clean_archive(tmp_path)
    legacy = tmp_path / "no-manifest.tgz"
    _copy_archive(clean, legacy, exclude=("manifest.json",))
    with pytest.raises(BackupError) as error:
        normalize_legacy_archive(legacy, tmp_path / "derived.tgz")
    assert error.value.code == "BACKUP_MANIFEST_INVALID"


def test_legacy_normalization_rejects_hash_mismatch(tmp_path):
    clean = _clean_archive(tmp_path)
    legacy = tmp_path / "mismatch.tgz"
    _copy_archive(clean, legacy, replace={"application.db": b"tampered"})
    with pytest.raises(BackupError) as error:
        normalize_legacy_archive(legacy, tmp_path / "derived.tgz")
    assert error.value.code == "BACKUP_HASH_MISMATCH"


@pytest.mark.parametrize(
    "member,payload",
    [
        (lambda: tarfile.TarInfo("../escape"), b"x"),
        (lambda: tarfile.TarInfo("/absolute"), b"x"),
        (lambda: tarfile.TarInfo("nested/file"), b"x"),
        (lambda: tarfile.TarInfo("manifest.json"), b"{}"),
        (lambda: _directory_member(), None),
        (lambda: _symlink_member(), None),
    ],
)
def test_legacy_normalization_rejects_unsafe_members(tmp_path, member, payload):
    legacy = _legacy_archive(tmp_path, extras=((member(), payload),))
    with pytest.raises(BackupError) as error:
        normalize_legacy_archive(legacy, tmp_path / "derived.tgz")
    assert error.value.code == "BACKUP_ARCHIVE_UNSAFE"


def _directory_member() -> tarfile.TarInfo:
    member = tarfile.TarInfo("nested")
    member.type = tarfile.DIRTYPE
    return member


def _symlink_member() -> tarfile.TarInfo:
    member = tarfile.TarInfo("escape-link")
    member.type = tarfile.SYMTYPE
    member.linkname = "application.db"
    return member


def test_legacy_normalization_rejects_corrupt_archive(tmp_path):
    corrupt = tmp_path / "corrupt.tgz"
    corrupt.write_bytes(b"not a gzip archive")
    with pytest.raises(BackupError) as error:
        normalize_legacy_archive(corrupt, tmp_path / "derived.tgz")
    assert error.value.code == "BACKUP_IMPORT_FAILED"
    assert not (tmp_path / "derived.tgz").exists()


def test_legacy_normalization_never_overwrites_derived_output(tmp_path):
    legacy = _legacy_archive(tmp_path)
    derived = tmp_path / "derived.tgz"
    derived.write_bytes(b"do not replace")
    with pytest.raises(BackupError) as error:
        normalize_legacy_archive(legacy, derived)
    assert error.value.code == "BACKUP_LEGACY_OUTPUT_EXISTS"
    assert derived.read_bytes() == b"do not replace"


def test_cli_normalize_legacy_is_explicit_and_strict_import_still_rejects(tmp_path):
    legacy = _legacy_archive(tmp_path)
    derived = tmp_path / "derived.tgz"
    receipt = tmp_path / "receipt.json"
    completed = _cli(
        "normalize-legacy",
        "--archive",
        str(legacy),
        "--out",
        str(derived),
        "--receipt",
        str(receipt),
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["schema"] == "delivery-recovery-legacy-normalization-v1"
    assert receipt.exists()
    assert derived.exists()

    strict = _cli("import", "--archive", str(legacy), "--target", str(tmp_path / "raw"))
    assert strict.returncode == 1
    assert json.loads(strict.stdout)["error"]["code"] == "BACKUP_ARCHIVE_UNEXPECTED_MEMBER"


def test_legacy_normalization_rejects_manifest_listed_sidecar(tmp_path):
    clean = _clean_archive(tmp_path)
    with tarfile.open(clean, "r:gz") as handle:
        manifest = json.loads(handle.extractfile("manifest.json").read())
    payload = b"hidden-wal-content"
    manifest["files"]["application.db-wal"] = hashlib.sha256(payload).hexdigest()
    legacy = tmp_path / "listed-wal.tgz"
    _copy_archive(
        clean,
        legacy,
        replace={"manifest.json": json.dumps(manifest).encode("utf-8")},
        extras=(_member("application.db-wal", payload),),
    )
    with pytest.raises(BackupError) as error:
        normalize_legacy_archive(legacy, tmp_path / "derived.tgz")
    assert error.value.code == "BACKUP_MANIFEST_INVALID"
    assert not (tmp_path / "derived.tgz").exists()


# --- Preservation: destinations must be distinct and never overwritten ---


def test_legacy_normalization_rejects_receipt_alias_of_source(tmp_path):
    legacy = _legacy_archive(tmp_path)
    before = legacy.read_bytes()
    with pytest.raises(BackupError) as error:
        normalize_legacy_archive(
            legacy, tmp_path / "derived.tgz", receipt_path=legacy
        )
    assert error.value.code == "BACKUP_LEGACY_PATH_CONFLICT"
    assert legacy.read_bytes() == before
    assert not (tmp_path / "derived.tgz").exists()


def test_legacy_normalization_rejects_receipt_alias_of_output(tmp_path):
    legacy = _legacy_archive(tmp_path)
    before = legacy.read_bytes()
    derived = tmp_path / "derived.tgz"
    with pytest.raises(BackupError) as error:
        normalize_legacy_archive(legacy, derived, receipt_path=derived)
    assert error.value.code == "BACKUP_LEGACY_PATH_CONFLICT"
    assert legacy.read_bytes() == before
    assert not derived.exists()


def test_legacy_normalization_rejects_output_alias_of_source(tmp_path):
    legacy = _legacy_archive(tmp_path)
    before = legacy.read_bytes()
    with pytest.raises(BackupError) as error:
        normalize_legacy_archive(legacy, legacy)
    assert error.value.code == "BACKUP_LEGACY_PATH_CONFLICT"
    assert legacy.read_bytes() == before


def test_legacy_normalization_rejects_hardlink_aliases(tmp_path):
    legacy = _legacy_archive(tmp_path)
    before = legacy.read_bytes()
    alias = tmp_path / "source-alias.tgz"
    try:
        os.link(legacy, alias)
    except OSError as exc:  # pragma: no cover - filesystem without hardlinks
        pytest.skip(f"hardlinks unavailable: {exc}")
    with pytest.raises(BackupError) as error:
        normalize_legacy_archive(
            legacy, tmp_path / "derived.tgz", receipt_path=alias
        )
    assert error.value.code == "BACKUP_LEGACY_PATH_CONFLICT"
    assert legacy.read_bytes() == before
    assert alias.read_bytes() == before
    assert not (tmp_path / "derived.tgz").exists()


def test_legacy_normalization_preserves_existing_receipt(tmp_path):
    legacy = _legacy_archive(tmp_path)
    before = legacy.read_bytes()
    receipt = tmp_path / "receipt.json"
    receipt.write_bytes(b"existing evidence; do not replace")
    derived = tmp_path / "derived.tgz"
    with pytest.raises(BackupError) as error:
        normalize_legacy_archive(legacy, derived, receipt_path=receipt)
    assert error.value.code == "BACKUP_LEGACY_RECEIPT_EXISTS"
    assert receipt.read_bytes() == b"existing evidence; do not replace"
    assert legacy.read_bytes() == before
    assert not derived.exists()


def test_legacy_normalization_preserves_predictable_staging_path(tmp_path):
    legacy = _legacy_archive(tmp_path)
    derived = tmp_path / "derived.tgz"
    colliding = derived.with_name(f"{derived.name}.tmp-{os.getpid()}")
    colliding.write_bytes(b"caller-owned staging path")
    result = normalize_legacy_archive(legacy, derived)
    assert colliding.read_bytes() == b"caller-owned staging path"
    assert result["normalized_sha256"] == hashlib.sha256(
        derived.read_bytes()
    ).hexdigest()


def test_legacy_normalization_receipt_matches_final_artifacts(tmp_path):
    legacy = _legacy_archive(tmp_path)
    derived = tmp_path / "derived.tgz"
    receipt = tmp_path / "receipt.json"
    result = normalize_legacy_archive(legacy, derived, receipt_path=receipt)
    recorded = json.loads(receipt.read_text(encoding="utf-8"))
    assert recorded == result
    assert recorded["original_archive"] == str(legacy)
    assert recorded["original_sha256"] == hashlib.sha256(
        legacy.read_bytes()
    ).hexdigest()
    assert recorded["original_bytes"] == legacy.stat().st_size
    assert recorded["normalized_archive"] == str(derived)
    assert recorded["normalized_sha256"] == hashlib.sha256(
        derived.read_bytes()
    ).hexdigest()
    assert recorded["normalized_bytes"] == derived.stat().st_size
    assert recorded["original_archive_unchanged"] is True


def test_cli_normalize_legacy_rejects_source_receipt_alias(tmp_path):
    legacy = _legacy_archive(tmp_path)
    before = legacy.read_bytes()
    derived = tmp_path / "derived.tgz"
    completed = _cli(
        "normalize-legacy",
        "--archive",
        str(legacy),
        "--out",
        str(derived),
        "--receipt",
        str(legacy),
    )
    assert completed.returncode == 1
    assert json.loads(completed.stdout)["error"]["code"] == "BACKUP_LEGACY_PATH_CONFLICT"
    assert "Traceback" not in completed.stderr
    assert legacy.read_bytes() == before
    assert not derived.exists()


def test_cli_normalize_legacy_preserves_existing_receipt(tmp_path):
    legacy = _legacy_archive(tmp_path)
    receipt = tmp_path / "receipt.json"
    receipt.write_bytes(b"keep this receipt")
    derived = tmp_path / "derived.tgz"
    completed = _cli(
        "normalize-legacy",
        "--archive",
        str(legacy),
        "--out",
        str(derived),
        "--receipt",
        str(receipt),
    )
    assert completed.returncode == 1
    assert json.loads(completed.stdout)["error"]["code"] == "BACKUP_LEGACY_RECEIPT_EXISTS"
    assert "Traceback" not in completed.stderr
    assert receipt.read_bytes() == b"keep this receipt"
    assert not derived.exists()
