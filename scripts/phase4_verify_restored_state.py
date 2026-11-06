"""Verify a locally restored delivery-recovery data set over HTTP.

The expected workflow inventory is read from the imported backup manifest, which
must live outside the restored application's writable data directory. Bearer
tokens are read from the restored identity file only in memory and are never
printed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx


def _load_manifest(path: Path) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"expected manifest is unreadable: {path}") from exc
    if not isinstance(document, dict):
        raise SystemExit("expected manifest root must be a JSON object")
    if document.get("schema") != "delivery-recovery-backup-v1":
        raise SystemExit("unexpected expected-manifest schema")
    runs = document.get("runs")
    if not isinstance(runs, list):
        raise SystemExit("expected manifest runs inventory is invalid")
    for key in ("run_count", "revision_count", "audit_event_count"):
        if not isinstance(document.get(key), int):
            raise SystemExit(f"expected manifest {key} is invalid")
    return document


def _load_identities(path: Path) -> dict[str, str]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"restored identity file is unreadable: {path}") from exc
    try:
        return {
            item["actor_id"]: item["token"]
            for item in document["identities"]
        }
    except (KeyError, TypeError) as exc:
        raise SystemExit("restored identity file is invalid") from exc


def verify(
    *,
    expected_manifest: Path,
    restored_data_dir: Path,
    base_url: str,
    client_factory=httpx.Client,
) -> dict:
    manifest = _load_manifest(expected_manifest)
    identities = _load_identities(restored_data_dir / "demo-identities.json")
    planner = identities.get("planner.demo")
    manager = identities.get("manager.demo")
    if not planner or not manager:
        raise SystemExit("required demo identities are missing")

    observed_runs: list[dict] = []
    events = 0
    revisions = 0
    with client_factory(base_url=base_url, timeout=10.0) as client:
        readiness = client.get("/readyz")
        readiness.raise_for_status()
        ready = readiness.json()
        if ready.get("status") != "ready":
            raise SystemExit(f"restored application is not ready: {ready}")
        for expected in manifest["runs"]:
            response = client.get(
                f"/v1/runs/{expected['run_id']}",
                headers={"Authorization": f"Bearer {planner}"},
            )
            response.raise_for_status()
            actual = response.json()
            observed = {
                "run_id": actual["run_id"],
                "status": actual["status"],
                "revision": actual["revision"],
                "decision_status": actual.get("decision_status"),
            }
            if (
                observed["run_id"] != expected["run_id"]
                or observed["status"] != expected["status"]
                or observed["revision"] != expected["revision"]
                or observed["decision_status"] != expected["decision_status"]
            ):
                raise SystemExit(
                    f"run mismatch for {expected['run_id']}: "
                    f"expected {expected!r}, observed {observed!r}"
                )
            observed_runs.append(observed)
        for expected in manifest["runs"]:
            response = client.get(
                f"/v1/runs/{expected['run_id']}/audit",
                headers={"Authorization": f"Bearer {manager}"},
            )
            response.raise_for_status()
            body = response.json()
            events += len(body["events"])
            revisions += len(body["revisions"])
    if events != manifest["audit_event_count"]:
        raise SystemExit(
            f"audit event count mismatch: expected {manifest['audit_event_count']}, observed {events}"
        )
    if revisions != manifest["revision_count"]:
        raise SystemExit(
            f"revision count mismatch: expected {manifest['revision_count']}, observed {revisions}"
        )
    return {
        "verified": True,
        "base_url": base_url,
        "expected_manifest": str(expected_manifest),
        "restored_data_dir": str(restored_data_dir),
        "run_count": len(observed_runs),
        "revision_count": revisions,
        "audit_event_count": events,
        "runs": observed_runs,
        "readiness": {
            "status": ready.get("status"),
            "storage": ready.get("storage"),
            "worker": ready.get("worker"),
            "schema_version": ready.get("schema_version"),
        },
        "tokens_recorded": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-manifest", required=True, type=Path)
    parser.add_argument("--restored-data-dir", required=True, type=Path)
    parser.add_argument("--base-url", required=True)
    args = parser.parse_args()
    result = verify(
        expected_manifest=args.expected_manifest,
        restored_data_dir=args.restored_data_dir,
        base_url=args.base_url,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
