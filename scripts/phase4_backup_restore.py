"""Create, verify and restore a consistent Phase 4 recovery asset."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from delivery_recovery.storage.backup import (  # noqa: E402
    BackupError,
    create_backup,
    export_backup,
    import_backup,
    normalize_legacy_archive,
    restore_backup,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create")
    create.add_argument("--data-dir", required=True)
    create.add_argument("--out", required=True)
    create.add_argument("--include-identity", action="store_true")

    restore = sub.add_parser("restore")
    restore.add_argument("--backup", required=True)
    restore.add_argument("--target", required=True)

    export = sub.add_parser("export")
    export.add_argument("--backup", required=True)
    export.add_argument("--archive", required=True)

    import_parser = sub.add_parser("import")
    import_parser.add_argument("--archive", required=True)
    import_parser.add_argument("--target", required=True)

    normalize = sub.add_parser(
        "normalize-legacy",
        help="Explicitly derive a strict-compatible archive from a legacy export.",
    )
    normalize.add_argument("--archive", required=True)
    normalize.add_argument("--out", required=True)
    normalize.add_argument("--receipt")

    args = parser.parse_args()
    try:
        if args.command == "create":
            result = create_backup(
                Path(args.data_dir),
                Path(args.out),
                include_identity=args.include_identity,
            )
        elif args.command == "restore":
            result = restore_backup(Path(args.backup), Path(args.target))
        elif args.command == "export":
            result = export_backup(Path(args.backup), Path(args.archive))
        elif args.command == "normalize-legacy":
            result = normalize_legacy_archive(
                Path(args.archive),
                Path(args.out),
                receipt_path=None if args.receipt is None else Path(args.receipt),
            )
        else:
            result = import_backup(Path(args.archive), Path(args.target))
    except BackupError as error:
        print(json.dumps({"error": {"code": error.code, "message": error.message}}))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
