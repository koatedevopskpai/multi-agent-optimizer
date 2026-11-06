"""Verify byte identity of this curated publication snapshot (no network)."""
from pathlib import Path
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]


def main():
    manifest = json.loads((ROOT / "docs/portfolio/release-manifest.json").read_text(encoding="utf-8"))
    failures = []
    for entry in manifest["files"]:
        path = (ROOT / entry["path"]).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file():
            failures.append(entry["path"])
        elif hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            failures.append(entry["path"])
    if failures:
        print("Changed or missing files: " + ", ".join(failures))
        return 1
    print(f"Verified {len(manifest['files'])} publication files; manifest excludes itself.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
