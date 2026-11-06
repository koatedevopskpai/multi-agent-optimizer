"""Consistent local backup and restore for the Phase 4 recovery assets.

The reference uses two SQLite stores. Both are copied through SQLite's backup
API rather than raw file copies so a committed WAL state is included. Restore
verifies the manifest hashes and database integrity before exposing the files.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "BackupError",
    "create_backup",
    "restore_backup",
    "export_backup",
    "import_backup",
    "normalize_legacy_archive",
]

REQUIRED_DATABASES = ("application.db", "checkpoints.db")
LEGACY_SIDECAR_SUFFIXES = ("-wal", "-shm")


class BackupError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _backup_sqlite(source: Path, destination: Path) -> None:
    connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(str(destination))
        try:
            connection.backup(target)
            target.commit()
        finally:
            target.close()
    finally:
        connection.close()


def _integrity(path: Path) -> str:
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return "failed"
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        return str(result[0]) if result else "unknown"
    except sqlite3.Error:
        return "failed"
    finally:
        connection.close()


def _run_inventory(database: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        runs = [
            {
                "run_id": row["run_id"],
                "status": row["status"],
                "revision": int(row["revision"]),
                "decision_status": row["decision_status"],
            }
            for row in connection.execute(
                "SELECT run_id, status, revision, decision_status FROM runs ORDER BY run_id"
            )
        ]
        revision_count = connection.execute(
            "SELECT COUNT(*) AS c FROM revisions"
        ).fetchone()["c"]
        audit_count = connection.execute(
            "SELECT COUNT(*) AS c FROM audit_events"
        ).fetchone()["c"]
    finally:
        connection.close()
    return {
        "runs": runs,
        "run_count": len(runs),
        "revision_count": int(revision_count),
        "audit_event_count": int(audit_count),
    }


def _validate_manifest_files(manifest: dict) -> dict[str, str]:
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise BackupError(
            "BACKUP_MANIFEST_INVALID", "manifest files must be an object."
        )
    for name, digest in files.items():
        if (
            not isinstance(name, str)
            or name in {"", ".", ".."}
            or Path(name).name != name
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise BackupError(
                "BACKUP_MANIFEST_INVALID", "manifest contains an invalid file entry."
            )
    missing = [name for name in REQUIRED_DATABASES if name not in files]
    if missing:
        raise BackupError(
            "BACKUP_MANIFEST_INVALID",
            f"manifest is missing required database(s): {', '.join(missing)}.",
        )
    return files


def _validate_manifest_counts(manifest: dict) -> None:
    for key in ("run_count", "revision_count", "audit_event_count"):
        value = manifest.get(key)
        if not isinstance(value, int) or value < 0:
            raise BackupError(
                "BACKUP_MANIFEST_INVALID", f"manifest {key} is invalid."
            )
    if not isinstance(manifest.get("runs"), list):
        raise BackupError(
            "BACKUP_MANIFEST_INVALID", "manifest runs inventory is invalid."
        )


def _manifest_from_bytes(data: bytes) -> dict:
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError(
            "BACKUP_MANIFEST_INVALID", "manifest.json is unreadable."
        ) from exc
    if not isinstance(document, dict):
        raise BackupError(
            "BACKUP_MANIFEST_INVALID", "manifest root must be a JSON object."
        )
    return document


def _load_manifest(path: Path) -> dict:
    if not path.is_file():
        raise BackupError("BACKUP_MANIFEST_INVALID", "manifest.json is missing.")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise BackupError(
            "BACKUP_MANIFEST_INVALID", "manifest.json is unreadable."
        ) from exc
    return _manifest_from_bytes(data)


def create_backup(
    data_dir: Path,
    backup_dir: Path,
    *,
    include_identity: bool = False,
    now: str | None = None,
) -> dict[str, Any]:
    data_dir = Path(data_dir)
    backup_dir = Path(backup_dir)
    application = data_dir / "application.db"
    checkpoints = data_dir / "checkpoints.db"
    if not application.is_file() or not checkpoints.is_file():
        raise BackupError(
            "BACKUP_SOURCE_INVALID",
            "application.db and checkpoints.db must both exist in the data directory.",
        )
    if backup_dir.exists() and any(backup_dir.iterdir()):
        raise BackupError(
            "BACKUP_DESTINATION_NOT_EMPTY",
            "The backup directory must be new or empty.",
        )
    backup_dir.mkdir(parents=True, exist_ok=True)
    try:
        _backup_sqlite(application, backup_dir / "application.db")
        _backup_sqlite(checkpoints, backup_dir / "checkpoints.db")
    except sqlite3.Error as exc:
        raise BackupError("BACKUP_FAILED", f"SQLite backup failed: {exc}") from exc

    files = ["application.db", "checkpoints.db"]
    if include_identity:
        identity = data_dir / "demo-identities.json"
        if identity.is_file():
            (backup_dir / "demo-identities.json").write_bytes(identity.read_bytes())
            files.append("demo-identities.json")

    try:
        inventory = _run_inventory(backup_dir / "application.db")
        integrity = {
            "application.db": _integrity(backup_dir / "application.db"),
            "checkpoints.db": _integrity(backup_dir / "checkpoints.db"),
        }
    except sqlite3.Error as exc:
        raise BackupError(
            "BACKUP_INTEGRITY_FAILED", f"Backup database cannot be read: {exc}"
        ) from exc
    if any(value != "ok" for value in integrity.values()):
        raise BackupError(
            "BACKUP_INTEGRITY_FAILED",
            "A backup database failed integrity_check.",
        )
    manifest = {
        "schema": "delivery-recovery-backup-v1",
        "created_at": now or datetime.now(timezone.utc).isoformat(),
        "source_data_dir": str(data_dir),
        "files": {
            name: _sha256(backup_dir / name)
            for name in files
        },
        "sqlite_integrity": integrity,
        **inventory,
        "identity_included": include_identity and "demo-identities.json" in files,
    }
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def restore_backup(backup_dir: Path, target_dir: Path) -> dict[str, Any]:
    backup_dir = Path(backup_dir)
    target_dir = Path(target_dir)
    manifest_path = backup_dir / "manifest.json"
    manifest = _load_manifest(manifest_path)
    if manifest.get("schema") != "delivery-recovery-backup-v1":
        raise BackupError("BACKUP_MANIFEST_INVALID", "Unsupported backup schema.")
    files = _validate_manifest_files(manifest)
    _validate_manifest_counts(manifest)
    if target_dir.exists() and any(target_dir.iterdir()):
        raise BackupError(
            "RESTORE_TARGET_NOT_EMPTY", "The restore target must be new or empty."
        )
    target_dir.mkdir(parents=True, exist_ok=True)

    for name, expected in files.items():
        source = backup_dir / name
        if not source.is_file():
            raise BackupError("BACKUP_FILE_MISSING", f"Backup file {name!r} is missing.")
        actual = _sha256(source)
        if actual != expected:
            raise BackupError(
                "BACKUP_HASH_MISMATCH",
                f"Backup file {name!r} does not match its manifest hash.",
            )
        (target_dir / name).write_bytes(source.read_bytes())

    integrity = {
        name: _integrity(target_dir / name)
        for name in REQUIRED_DATABASES
    }
    if any(value != "ok" for value in integrity.values()):
        raise BackupError(
            "BACKUP_INTEGRITY_FAILED", "A restored database failed integrity_check."
        )
    try:
        inventory = _run_inventory(target_dir / "application.db")
    except sqlite3.Error as exc:
        raise BackupError(
            "BACKUP_INTEGRITY_FAILED", f"Restored database cannot be read: {exc}"
        ) from exc
    expected_inventory = {
        "runs": manifest.get("runs"),
        "run_count": manifest.get("run_count"),
        "revision_count": manifest.get("revision_count"),
        "audit_event_count": manifest.get("audit_event_count"),
    }
    if inventory != expected_inventory:
        raise BackupError(
            "BACKUP_INVENTORY_MISMATCH",
            "Restored workflow inventory does not match the backup manifest.",
        )
    result = {
        "schema": "delivery-recovery-restore-v1",
        "integrity": "ok",
        "integrity_detail": integrity,
        "target_data_dir": str(target_dir),
        **inventory,
    }
    (target_dir / "restore-manifest.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return result


def export_backup(backup_dir: Path, archive_path: Path) -> dict[str, Any]:
    """Archive a verified backup directory for off-instance transfer."""

    backup_dir = Path(backup_dir)
    archive_path = Path(archive_path)
    manifest_path = backup_dir / "manifest.json"
    manifest = _load_manifest(manifest_path)
    if manifest.get("schema") != "delivery-recovery-backup-v1":
        raise BackupError("BACKUP_MANIFEST_INVALID", "Unsupported backup schema.")
    files = _validate_manifest_files(manifest)
    _validate_manifest_counts(manifest)
    for name, expected in files.items():
        source = backup_dir / name
        if not source.is_file():
            raise BackupError("BACKUP_FILE_MISSING", f"Backup file {name!r} is missing.")
        if _sha256(source) != expected:
            raise BackupError(
                "BACKUP_HASH_MISMATCH",
                f"Backup file {name!r} does not match its manifest hash.",
            )
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        raise BackupError(
            "BACKUP_ARCHIVE_EXISTS", "The archive path already exists."
        )
    try:
        with tarfile.open(archive_path, "w:gz") as archive:
            for name in ["manifest.json", *sorted(files)]:
                archive.add(backup_dir / name, arcname=name)
    except OSError as exc:
        raise BackupError("BACKUP_EXPORT_FAILED", f"Archive failed: {exc}") from exc
    return {
        "schema": "delivery-recovery-export-v1",
        "archive": str(archive_path),
        "sha256": _sha256(archive_path),
        "bytes": archive_path.stat().st_size,
        "files": sorted(manifest.get("files", {})),
    }


def import_backup(archive_path: Path, target_dir: Path) -> dict[str, Any]:
    """Extract a transferred archive after strict member and manifest validation."""

    archive_path = Path(archive_path)
    target_dir = Path(target_dir)
    if not archive_path.is_file():
        raise BackupError("BACKUP_ARCHIVE_MISSING", "The archive is missing.")
    if target_dir.exists() and any(target_dir.iterdir()):
        raise BackupError(
            "BACKUP_TARGET_NOT_EMPTY", "The import target must be new or empty."
        )
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            names: list[str] = []
            for member in members:
                if not member.isfile():
                    raise BackupError(
                        "BACKUP_ARCHIVE_UNSAFE",
                        f"Unsupported archive member type: {member.name!r}.",
                    )
                member_path = Path(member.name)
                if (
                    member_path.is_absolute()
                    or ".." in member_path.parts
                    or len(member_path.parts) != 1
                    or member.name in names
                ):
                    raise BackupError(
                        "BACKUP_ARCHIVE_UNSAFE",
                        f"Unsafe archive member {member.name!r}.",
                    )
                names.append(member.name)
            if "manifest.json" not in names:
                raise BackupError(
                    "BACKUP_MANIFEST_INVALID", "manifest.json is missing from the archive."
                )
            manifest_member = archive.extractfile("manifest.json")
            if manifest_member is None:
                raise BackupError(
                    "BACKUP_MANIFEST_INVALID", "manifest.json is unreadable."
                )
            manifest = _manifest_from_bytes(manifest_member.read())
            if manifest.get("schema") != "delivery-recovery-backup-v1":
                raise BackupError("BACKUP_MANIFEST_INVALID", "Unsupported backup schema.")
            files = _validate_manifest_files(manifest)
            _validate_manifest_counts(manifest)
            expected = set(files) | {"manifest.json"}
            actual = set(names)
            unexpected = actual - expected
            if unexpected:
                raise BackupError(
                    "BACKUP_ARCHIVE_UNEXPECTED_MEMBER",
                    f"Unexpected archive member(s): {', '.join(sorted(unexpected))}.",
                )
            missing = expected - actual
            if missing:
                raise BackupError(
                    "BACKUP_ARCHIVE_MISSING_MEMBER",
                    f"Missing archive member(s): {', '.join(sorted(missing))}.",
                )
            target_dir.mkdir(parents=True, exist_ok=True)
            for name in sorted(expected):
                member = archive.getmember(name)
                source = archive.extractfile(member)
                if source is None:
                    raise BackupError(
                        "BACKUP_IMPORT_FAILED", f"Archive member {name!r} is unreadable."
                    )
                (target_dir / name).write_bytes(source.read())
            for name, digest in files.items():
                if _sha256(target_dir / name) != digest:
                    raise BackupError(
                        "BACKUP_HASH_MISMATCH",
                        f"Imported file {name!r} does not match the manifest.",
                    )
    except BackupError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise BackupError("BACKUP_IMPORT_FAILED", f"Archive extract failed: {exc}") from exc
    return {
        "schema": "delivery-recovery-import-v1",
        "archive": str(archive_path),
        "target": str(target_dir),
        "files": sorted(files),
    }


def _legacy_sidecar_kind(name: str) -> str | None:
    """Return "wal"/"shm" for documented legacy SQLite sidecars, else None."""

    for base in REQUIRED_DATABASES:
        for suffix in LEGACY_SIDECAR_SUFFIXES:
            if name == f"{base}{suffix}":
                return suffix[1:]
    return None



def _same_path(left: Path, right: Path) -> bool:
    """Return True for identical/resolved/hardlinked paths."""

    try:
        if left.exists() and right.exists():
            return os.path.samefile(left, right)
    except OSError:
        pass
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def _validate_legacy_destinations(
    source: Path, output: Path, receipt: Path | None
) -> None:
    pairs = [("source/output", source, output)]
    if receipt is not None:
        pairs.extend(
            [("source/receipt", source, receipt), ("output/receipt", output, receipt)]
        )
    for label, left, right in pairs:
        if _same_path(left, right):
            raise BackupError(
                "BACKUP_LEGACY_PATH_CONFLICT",
                f"Legacy normalization paths must be distinct; {label} refer to "
                "the same file.",
            )
    if output.exists():
        raise BackupError(
            "BACKUP_LEGACY_OUTPUT_EXISTS",
            "The normalized archive path already exists; refusing to overwrite it.",
        )
    if receipt is not None and receipt.exists():
        raise BackupError(
            "BACKUP_LEGACY_RECEIPT_EXISTS",
            "The normalization receipt path already exists; refusing to overwrite it.",
        )


def normalize_legacy_archive(
    archive_path: Path,
    output_path: Path,
    *,
    receipt_path: Path | None = None,
) -> dict[str, Any]:
    """Create a strict-import-compatible derivative of a legacy export.

    This is an explicit compatibility step for archives produced by the
    ``db4a072`` exporter, which packaged unlisted SQLite ``-wal``/``-shm``
    sidecars. The original archive is never modified and ordinary
    :func:`import_backup` continues to reject unlisted members.

    Only empty WALs and their paired SHM files for the two required databases are
    accepted and excluded. Any other extra member, nonempty WAL, unsafe or
    duplicate member, missing required file, or manifest hash mismatch aborts
    without writing the derivative.
    """

    archive_path = Path(archive_path)
    output_path = Path(output_path)
    receipt_path = None if receipt_path is None else Path(receipt_path)
    if not archive_path.is_file():
        raise BackupError("BACKUP_ARCHIVE_MISSING", "The archive is missing.")
    _validate_legacy_destinations(archive_path, output_path, receipt_path)
    try:
        original_sha256 = _sha256(archive_path)
        original_bytes = archive_path.stat().st_size
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if receipt_path is not None:
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            dir=str(output_path.parent),
        )
        os.close(descriptor)
    except OSError as exc:
        raise BackupError(
            "BACKUP_IMPORT_FAILED", f"Legacy archive normalization failed: {exc}"
        ) from exc
    temporary = Path(temporary_name)
    if (
        _same_path(temporary, archive_path)
        or _same_path(temporary, output_path)
        or (receipt_path is not None and _same_path(temporary, receipt_path))
    ):
        temporary.unlink(missing_ok=True)
        raise BackupError(
            "BACKUP_LEGACY_PATH_CONFLICT",
            "The staging path collides with a protected destination.",
        )
    excluded: list[dict[str, Any]] = []
    output_created = False
    receipt_created = False
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            names: list[str] = []
            for member in archive.getmembers():
                if not member.isfile():
                    raise BackupError(
                        "BACKUP_ARCHIVE_UNSAFE",
                        f"Unsupported archive member type: {member.name!r}.",
                    )
                member_path = Path(member.name)
                if (
                    member_path.is_absolute()
                    or ".." in member_path.parts
                    or len(member_path.parts) != 1
                    or member.name in names
                ):
                    raise BackupError(
                        "BACKUP_ARCHIVE_UNSAFE",
                        f"Unsafe archive member {member.name!r}.",
                    )
                names.append(member.name)
            if "manifest.json" not in names:
                raise BackupError(
                    "BACKUP_MANIFEST_INVALID",
                    "manifest.json is missing from the archive.",
                )
            manifest_member = archive.extractfile("manifest.json")
            if manifest_member is None:
                raise BackupError(
                    "BACKUP_MANIFEST_INVALID", "manifest.json is unreadable."
                )
            manifest = _manifest_from_bytes(manifest_member.read())
            if manifest.get("schema") != "delivery-recovery-backup-v1":
                raise BackupError(
                    "BACKUP_MANIFEST_INVALID", "Unsupported backup schema."
                )
            files = _validate_manifest_files(manifest)
            _validate_manifest_counts(manifest)
            for name in files:
                if _legacy_sidecar_kind(name) is not None:
                    raise BackupError(
                        "BACKUP_MANIFEST_INVALID",
                        "Manifest must not list SQLite -wal/-shm sidecars as files.",
                    )
            expected = set(files) | {"manifest.json"}
            actual = set(names)
            for name in sorted(actual - expected):
                kind = _legacy_sidecar_kind(name)
                if kind is None:
                    raise BackupError(
                        "BACKUP_ARCHIVE_UNEXPECTED_MEMBER",
                        f"Unexpected archive member(s): {name}.",
                    )
                member = archive.getmember(name)
                if kind == "wal" and member.size != 0:
                    raise BackupError(
                        "BACKUP_LEGACY_WAL_NONEMPTY",
                        f"Legacy WAL {name!r} is not empty ({member.size} bytes); "
                        "refusing to discard possible committed data.",
                    )
                excluded.append(
                    {"name": name, "bytes": member.size, "kind": kind}
                )
            missing = expected - actual
            if missing:
                raise BackupError(
                    "BACKUP_ARCHIVE_MISSING_MEMBER",
                    f"Missing archive member(s): {', '.join(sorted(missing))}.",
                )
            for name, digest in files.items():
                source = archive.extractfile(name)
                if source is None:
                    raise BackupError(
                        "BACKUP_IMPORT_FAILED",
                        f"Archive member {name!r} is unreadable.",
                    )
                if hashlib.sha256(source.read()).hexdigest() != digest:
                    raise BackupError(
                        "BACKUP_HASH_MISMATCH",
                        f"Archive member {name!r} does not match the manifest.",
                    )
            with tarfile.open(temporary, "w:gz") as target:
                for name in sorted(expected):
                    source = archive.extractfile(name)
                    if source is None:
                        raise BackupError(
                            "BACKUP_IMPORT_FAILED",
                            f"Archive member {name!r} is unreadable.",
                        )
                    target.addfile(archive.getmember(name), source)
        if _sha256(archive_path) != original_sha256:
            raise BackupError(
                "BACKUP_LEGACY_ORIGINAL_CHANGED",
                "The original archive changed during normalization.",
            )
        normalized_sha256 = _sha256(temporary)
        normalized_bytes = temporary.stat().st_size
        result = {
            "schema": "delivery-recovery-legacy-normalization-v1",
            "operation": "explicit legacy-export normalization; strict importer unchanged",
            "original_archive": str(archive_path),
            "original_sha256": original_sha256,
            "original_bytes": original_bytes,
            "normalized_archive": str(output_path),
            "normalized_sha256": normalized_sha256,
            "normalized_bytes": normalized_bytes,
            "included_members": sorted(expected),
            "excluded_members": [item["name"] for item in excluded],
            "excluded_member_details": excluded,
            "excluded_wal_sizes_zero": all(
                item["bytes"] == 0 for item in excluded if item["kind"] == "wal"
            ),
            "all_manifest_file_hashes_match": True,
            "original_archive_unchanged": True,
        }
        if output_path.exists():
            raise BackupError(
                "BACKUP_LEGACY_OUTPUT_EXISTS",
                "The normalized archive path appeared during normalization; "
                "refusing to overwrite it.",
            )
        os.replace(temporary, output_path)
        output_created = True
        if receipt_path is not None:
            try:
                handle = receipt_path.open("x", encoding="utf-8", newline="\n")
            except FileExistsError as exc:
                raise BackupError(
                    "BACKUP_LEGACY_RECEIPT_EXISTS",
                    "The normalization receipt path appeared during normalization; "
                    "refusing to overwrite it.",
                ) from exc
            receipt_created = True
            with handle:
                handle.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
        return result
    except BackupError:
        temporary.unlink(missing_ok=True)
        if receipt_created and receipt_path is not None:
            receipt_path.unlink(missing_ok=True)
        if output_created:
            output_path.unlink(missing_ok=True)
        raise
    except (OSError, tarfile.TarError) as exc:
        temporary.unlink(missing_ok=True)
        if receipt_created and receipt_path is not None:
            receipt_path.unlink(missing_ok=True)
        if output_created:
            output_path.unlink(missing_ok=True)
        raise BackupError(
            "BACKUP_IMPORT_FAILED", f"Legacy archive normalization failed: {exc}"
        ) from exc
