"""Bounded operator-guarded EC2 restore window.

This is the sibling of ``phase4_backup_window.py``. It starts the stopped
development instance, verifies a retained archive locally, uploads it over SSM,
restores it into a new isolated directory on the encrypted data disk, switches
the stable ``current`` path, verifies application state, restarts the service,
optionally exercises rollback, and always stops compute.

It reuses the accepted identity, deadline, emergency-stop and process-cleanup
guards. The module never contacts AWS at import or in tests; only ``main`` can
construct the real runner, and even then every call goes through the reviewed
stage wrapper with the operator profile. It never falls back to default/Admin
credentials.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import posixpath
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from phase4_backup_window import (  # noqa: E402
    DEFAULT_DEADLINE_SECONDS,
    DEFAULT_STOP_RESERVE_SECONDS,
    WindowError,
    _default_port_ready,
    _instance_state,
    _remaining,
    _run_process,
    _run_stage,
    _ssm_poll,
    _ssm_send,
    _stage_command,
    _terminate_process,
    _wait,
)

MAX_UPLOAD_BYTES = 200_000
UPLOAD_CHUNK_CHARS = 60_000
MAX_SSM_PAYLOAD_CHARS = 80_000
REMOTE_DATA_ROOT = "/srv/delivery-recovery/data"
REMOTE_INCOMING = "/srv/delivery-recovery/incoming"
SERVICE_CONTROL = "/usr/local/lib/delivery-recovery/service-control.sh"
RESTORE_SWITCH = "/usr/local/lib/delivery-recovery/restore-switch.sh"
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ECR_IMAGE_REF_PATTERN = re.compile(
    r"^\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/"
    r"[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}$"
)


@dataclass(frozen=True)
class RestoreConfig:
    account_id: str
    instance_id: str
    run_id: str
    archive_path: Path
    archive_sha256: str
    expected_manifest: Path
    identity_dir: Path
    image_ref: str
    release_id: str
    rollback: bool = False
    local_port: int = 18081
    region: str = "eu-west-2"
    deadline_seconds: int = DEFAULT_DEADLINE_SECONDS
    stop_reserve_seconds: int = DEFAULT_STOP_RESERVE_SECONDS


@dataclass
class RestoreState:
    failure_stage: str | None = None
    restore_verified: bool = False
    service_restarted: bool = False
    destinations_checked: bool = False
    switch_attempted: bool = False
    switch_completed: bool = False
    switch_uncertain: bool = False
    switch_state: str | None = None
    rollback_attempted: bool = False
    rollback_ok: bool | None = None
    verification: dict[str, Any] | None = None
    previous_logical_state: dict[str, Any] | None = None
    cleanup_errors: list[str] = field(default_factory=list)


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _validate_name(value: str, label: str) -> None:
    if not RUN_ID_PATTERN.fullmatch(value or ""):
        raise WindowError(
            "INVALID_ARGUMENT",
            f"{label} must be a simple name (letters, digits, '.', '_', '-').",
        )


def _load_json(path: Path, label: str) -> dict:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WindowError("PREFLIGHT_FAILED", f"{label} is unreadable: {exc}") from exc
    if not isinstance(document, dict):
        raise WindowError("PREFLIGHT_FAILED", f"{label} root must be an object")
    return document


def local_preflight(config: RestoreConfig) -> dict[str, Any]:
    """Validate local artifacts before contacting AWS."""

    _validate_name(config.run_id, "run id")
    _validate_name(config.release_id, "release id")
    if not ECR_IMAGE_REF_PATTERN.fullmatch(config.image_ref or ""):
        raise WindowError(
            "PREFLIGHT_FAILED",
            "image reference must be a full ECR "
            "<account>.dkr.ecr.<region>.amazonaws.com/<repo>@sha256:<64 hex> "
            "digest; an unqualified local image name is not accepted",
        )
    archive = Path(config.archive_path)
    if not archive.is_file():
        raise WindowError("ARCHIVE_MISSING", "the retained archive is missing")
    size = archive.stat().st_size
    if not 0 < size <= MAX_UPLOAD_BYTES:
        raise WindowError(
            "ARCHIVE_SIZE_INVALID",
            f"archive must be 1..{MAX_UPLOAD_BYTES} bytes for the SSM upload route",
        )
    actual = _sha256(archive)
    if actual != config.archive_sha256:
        raise WindowError(
            "ARCHIVE_HASH_MISMATCH",
            f"local archive hash {actual} does not match the expected archive hash",
        )
    manifest = _load_json(config.expected_manifest, "expected manifest")
    if manifest.get("schema") != "delivery-recovery-backup-v1":
        raise WindowError("PREFLIGHT_FAILED", "unexpected expected-manifest schema")
    for key in ("run_count", "revision_count", "audit_event_count"):
        if not isinstance(manifest.get(key), int) or manifest[key] < 0:
            raise WindowError("PREFLIGHT_FAILED", f"expected manifest {key} is invalid")
    if not isinstance(manifest.get("runs"), list):
        raise WindowError("PREFLIGHT_FAILED", "expected manifest runs are invalid")
    identity = Path(config.identity_dir) / "demo-identities.json"
    document = _load_json(identity, "identity file")
    try:
        actors = {item["actor_id"] for item in document["identities"]}
    except (KeyError, TypeError) as exc:
        raise WindowError("PREFLIGHT_FAILED", "identity file is invalid") from exc
    if not {"planner.demo", "manager.demo"}.issubset(actors):
        raise WindowError(
            "PREFLIGHT_FAILED", "required demo identities are missing"
        )
    return {
        "archive_sha256": actual,
        "archive_bytes": size,
        "image_ref": config.image_ref,
        "release_id": config.release_id,
        "expected_run_count": manifest["run_count"],
        "expected_revision_count": manifest["revision_count"],
        "expected_audit_event_count": manifest["audit_event_count"],
    }


def _upload_commands(
    payload: bytes, remote_path: str
) -> list[list[str]]:
    encoded = base64.b64encode(payload).decode("ascii")
    chunks = [
        encoded[index : index + UPLOAD_CHUNK_CHARS]
        for index in range(0, len(encoded), UPLOAD_CHUNK_CHARS)
    ] or [""]
    remote_dir = posixpath.dirname(remote_path)
    commands = [
        f"install -d -m 0700 -o 10001 -g 10001 '{remote_dir}'",
        f"test ! -e '{remote_path}'",
    ]
    for index, chunk in enumerate(chunks):
        operator = ">" if index == 0 else ">>"
        commands.append(f"printf '%s' '{chunk}' | base64 -d {operator} '{remote_path}'")
    commands.extend(
        [
            f"chown 10001:10001 '{remote_path}'",
            f"sha256sum '{remote_path}'",
            f"test \"$(sha256sum '{remote_path}' | cut -d' ' -f1)\" = "
            f"'{hashlib.sha256(payload).hexdigest()}'",
        ]
    )
    batches: list[list[str]] = []
    current: list[str] = []
    for command in commands:
        candidate = [*current, command]
        if current and len(json.dumps({"commands": candidate})) > MAX_SSM_PAYLOAD_CHARS:
            batches.append(current)
            current = [command]
        else:
            current = candidate
    if current:
        batches.append(current)
    return [["set -e", *batch] for batch in batches]


def _upload_archive(
    config: RestoreConfig,
    *,
    deadline: float,
    clock: Callable[[], float],
    runner: Callable,
    sleep: Callable[[float], None],
) -> None:
    data = Path(config.archive_path).read_bytes()
    remote_path = f"{REMOTE_INCOMING}/{config.run_id}.tgz"
    output = ""
    for batch in _upload_commands(data, remote_path):
        command_id = _ssm_send(
            config,
            stage="restore-upload",
            comment="upload verified restore archive",
            commands=batch,
            deadline=deadline,
            clock=clock,
            runner=runner,
        )
        output = _ssm_poll(
            config,
            stage="restore-upload",
            command_id=command_id,
            deadline=deadline,
            clock=clock,
            runner=runner,
            sleep=sleep,
        )
    remote_digest = output.strip().split()[0] if output.strip() else ""
    if remote_digest != config.archive_sha256:
        raise WindowError(
            "UPLOAD_HASH_MISMATCH",
            f"remote upload hash {remote_digest or 'missing'} does not match local",
        )


def _restore_commands(config: RestoreConfig) -> list[str]:
    restore_root = f"{REMOTE_DATA_ROOT}/restore/{config.run_id}"
    incoming = f"{REMOTE_INCOMING}/{config.run_id}.tgz"
    return [
        "set -e",
        f"test ! -e '{restore_root}'",
        f"install -d -m 0700 -o 10001 -g 10001 '{restore_root}'",
        "docker run --rm --read-only --tmpfs /tmp:size=64m,mode=1777 "
        "--user 10001:10001 "
        f"-v '{REMOTE_INCOMING}:/backup' -v '{restore_root}:/target' "
        f"'{config.image_ref}' sh -lc "
        "'python scripts/phase4_backup_restore.py import "
        f"--archive /backup/{config.run_id}.tgz --target /target/imported && "
        "python scripts/phase4_backup_restore.py restore "
        "--backup /target/imported --target /target/restored'",
    ]


def _require_restore_output(output: str) -> None:
    if '"delivery-recovery-import-v1"' not in output:
        raise WindowError("RESTORE_FAILED", "import output was not observed")
    if '"delivery-recovery-restore-v1"' not in output:
        raise WindowError("RESTORE_FAILED", "restore output was not observed")
    if '"integrity": "ok"' not in output:
        raise WindowError("RESTORE_FAILED", "restore integrity was not ok")


def _check_destinations(
    config: RestoreConfig,
    *,
    deadline: float,
    clock: Callable[[], float],
    runner: Callable,
    sleep: Callable[[float], None],
) -> None:
    """Refuse collisions before this attempt can move any data."""

    incoming = f"{REMOTE_INCOMING}/{config.run_id}.tgz"
    restore_root = f"{REMOTE_DATA_ROOT}/restore/{config.run_id}"
    versions = f"{REMOTE_DATA_ROOT}/versions/{config.release_id}"
    command = (
        f"for p in '{incoming}' '{restore_root}' '{versions}'; do "
        "[ -e \"$p\" ] && echo \"$p\"; done; true"
    )
    command_id = _ssm_send(
        config,
        stage="restore-destination-preflight",
        comment="check restore destinations",
        commands=[command],
        deadline=deadline,
        clock=clock,
        runner=runner,
    )
    output = _ssm_poll(
        config,
        stage="restore-destination-preflight",
        command_id=command_id,
        deadline=deadline,
        clock=clock,
        runner=runner,
        sleep=sleep,
    )
    conflicts = [line.strip() for line in output.splitlines() if line.strip()]
    if conflicts:
        raise WindowError(
            "DESTINATION_CONFLICT",
            "refusing restore because destination(s) already exist: "
            + ", ".join(conflicts),
        )


def _switch_state(
    config: RestoreConfig,
    *,
    deadline: float,
    clock: Callable[[], float],
    runner: Callable,
    sleep: Callable[[float], None],
) -> str:
    restore_root = f"{REMOTE_DATA_ROOT}/restore/{config.run_id}"
    versions = f"{REMOTE_DATA_ROOT}/versions/{config.release_id}"
    current = f"{REMOTE_DATA_ROOT}/current"
    restored = f"{restore_root}/restored"
    command = (
        f"if [ -d '{versions}' ]; then "
        f"if [ ! -d '{current}' ]; then echo previous_retained; "
        f"elif [ ! -e '{restored}' ]; then echo activated; "
        "else echo uncertain; fi; "
        f"elif [ -e '{restored}' ]; then echo not_activated; "
        "else echo uncertain; fi"
    )
    command_id = _ssm_send(
        config,
        stage="restore-switch-state",
        comment="reconcile switch outcome",
        commands=[command],
        deadline=deadline,
        clock=clock,
        runner=runner,
    )
    output = _ssm_poll(
        config,
        stage="restore-switch-state",
        command_id=command_id,
        deadline=deadline,
        clock=clock,
        runner=runner,
        sleep=sleep,
    )
    observed = output.strip().splitlines()[-1].strip() if output.strip() else "uncertain"
    if observed not in {
        "activated",
        "previous_retained",
        "not_activated",
        "uncertain",
    }:
        observed = "uncertain"
    return observed


def _switch(config: RestoreConfig, state: RestoreState, **kwargs) -> None:
    restore_root = f"{REMOTE_DATA_ROOT}/restore/{config.run_id}"
    state.switch_attempted = True
    try:
        command_id = _ssm_send(
            config,
            stage="restore-switch",
            comment="activate verified restore data",
            commands=[
                f"{RESTORE_SWITCH} switch '{restore_root}/restored' "
                f"'{config.release_id}'"
            ],
            deadline=kwargs["deadline"],
            clock=kwargs["clock"],
            runner=kwargs["runner"],
        )
        output = _ssm_poll(
            config,
            stage="restore-switch",
            command_id=command_id,
            deadline=kwargs["deadline"],
            clock=kwargs["clock"],
            runner=kwargs["runner"],
            sleep=kwargs["sleep"],
        )
        if "restore-switch: activated" not in output:
            raise WindowError(
                "SWITCH_FAILED", "restore-switch did not report activation"
            )
        state.switch_completed = True
        state.switch_state = "activated"
        return
    except WindowError as exc:
        try:
            observed = _switch_state(
                config,
                deadline=kwargs["deadline"],
                clock=kwargs["clock"],
                runner=kwargs["runner"],
                sleep=kwargs["sleep"],
            )
        except Exception as probe_exc:  # noqa: BLE001 - uncertain is truthful
            state.switch_uncertain = True
            state.switch_state = "uncertain"
            raise WindowError(
                "SWITCH_STATE_UNCERTAIN",
                "switch outcome could not be reconciled; preserve current/versions "
                f"and inspect before retrying: {probe_exc}",
            ) from exc
        state.switch_state = observed
        if observed == "activated":
            state.switch_completed = True
            return
        if observed == "previous_retained":
            state.switch_completed = True
            state.switch_uncertain = True
            raise WindowError(
                "SWITCH_PARTIAL",
                "switch moved current but did not complete; previous data is "
                "retained and will be recovered",
            ) from exc
        if observed == "not_activated":
            state.switch_completed = False
            raise WindowError(
                "SWITCH_FAILED",
                f"switch did not activate: {exc.message}",
            ) from exc
        state.switch_uncertain = True
        raise WindowError(
            "SWITCH_STATE_UNCERTAIN",
            "switch outcome is uncertain; preserve current/versions and inspect "
            "before retrying",
        ) from exc


def _service_action(
    config: RestoreConfig,
    action: str,
    *,
    stage: str,
    deadline: float,
    clock: Callable[[], float],
    runner: Callable,
    sleep: Callable[[float], None],
    confirm_stopped: bool = False,
) -> None:
    commands = ["set -e", f"{SERVICE_CONTROL} {action}"]
    if confirm_stopped:
        commands.append(f"test \"$({SERVICE_CONTROL} status)\" != running")
    command_id = _ssm_send(
        config,
        stage=stage,
        comment=f"service {action}",
        commands=commands,
        deadline=deadline,
        clock=clock,
        runner=runner,
    )
    _ssm_poll(
        config,
        stage=stage,
        command_id=command_id,
        deadline=deadline,
        clock=clock,
        runner=runner,
        sleep=sleep,
    )


def _logical_state_code() -> str:
    # SQLite WAL mode needs a writable -shm even for a read-only database. A
    # cleanly stopped service often leaves no -shm, and the source is mounted
    # read-only, so copy the DB and any sidecars to writable tmpfs first. The
    # live source is never modified; committed WAL data is preserved.
    return (
        "import json,os,shutil,sqlite3; "
        'src="/data"; dst="/tmp/restore-state"; os.makedirs(dst, exist_ok=True); '
        'names=("application.db","application.db-wal","application.db-shm"); '
        '[(shutil.copyfile(os.path.join(src,n), os.path.join(dst,n)), '
        'os.chmod(os.path.join(dst,n), 0o600)) '
        'for n in names if os.path.exists(os.path.join(src,n))]; '
        'con=sqlite3.connect("file:/tmp/restore-state/application.db?mode=rw", uri=True); '
        'runs=[{"run_id":r[0],"status":r[1],"revision":int(r[2]),'
        '"decision_status":r[3]} for r in con.execute('
        '"SELECT run_id,status,revision,decision_status FROM runs ORDER BY run_id")]; '
        'rev=con.execute("SELECT COUNT(*) FROM revisions").fetchone()[0]; '
        'aud=con.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]; '
        'integ=con.execute("PRAGMA integrity_check").fetchone()[0]; '
        'print(json.dumps({"runs":runs,"run_count":len(runs),'
        '"revision_count":int(rev),"audit_event_count":int(aud),'
        '"integrity":integ},sort_keys=True))'
    )


def _logical_state_command(image_ref: str, data_mount: str | None = None) -> str:
    mount = data_mount or f"{REMOTE_DATA_ROOT}/current"
    return (
        "docker run --rm --read-only --tmpfs /tmp:size=64m,mode=1777 "
        "--user 10001:10001 --entrypoint python "
        f"-v '{mount}:/data:ro' '{image_ref}' -c '{_logical_state_code()}'"
    )


def _logical_state(
    config: RestoreConfig,
    *,
    stage: str,
    deadline: float,
    clock: Callable[[], float],
    runner: Callable,
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    command = _logical_state_command(config.image_ref)
    command_id = _ssm_send(
        config,
        stage=stage,
        comment="read logical current state",
        commands=[command],
        deadline=deadline,
        clock=clock,
        runner=runner,
    )
    output = _ssm_poll(
        config,
        stage=stage,
        command_id=command_id,
        deadline=deadline,
        clock=clock,
        runner=runner,
        sleep=sleep,
    )
    document = None
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            document = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    if not isinstance(document, dict):
        raise WindowError("STATE_READ_FAILED", "logical state was not readable")
    for key in ("run_count", "revision_count", "audit_event_count"):
        if not isinstance(document.get(key), int) or document[key] < 0:
            raise WindowError("STATE_READ_FAILED", f"logical {key} is invalid")
    if not isinstance(document.get("runs"), list):
        raise WindowError("STATE_READ_FAILED", "logical runs are invalid")
    if document.get("integrity") != "ok":
        raise WindowError("STATE_READ_FAILED", "logical integrity_check was not ok")
    return document


def _state_summary(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_count": state.get("run_count"),
        "revision_count": state.get("revision_count"),
        "audit_event_count": state.get("audit_event_count"),
        "integrity": state.get("integrity"),
    }


def _default_verify(
    expected_manifest: Path,
    identity_dir: Path,
    base_url: str,
    timeout: float,
) -> dict:
    command = [
        sys.executable,
        str(REPO / "scripts" / "phase4_verify_restored_state.py"),
        "--expected-manifest",
        str(expected_manifest),
        "--restored-data-dir",
        str(identity_dir),
        "--base-url",
        base_url,
    ]
    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=max(1.0, timeout)
    )
    if completed.returncode != 0:
        raise WindowError(
            "VERIFY_FAILED", (completed.stderr or completed.stdout or "")[:400]
        )
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise WindowError("VERIFY_FAILED", "verifier returned invalid JSON") from exc
    if not isinstance(document, dict) or document.get("verified") is not True:
        raise WindowError("VERIFY_FAILED", "verifier did not report verified state")
    return document


def _default_ready(base_url: str) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"{base_url}/readyz", timeout=3) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read())
            return payload.get("status") == "ready"
    except Exception:
        return False


def _verify_state(
    config: RestoreConfig,
    *,
    base_url: str,
    verify: Callable[..., dict],
    deadline: float,
    clock: Callable[[], float],
) -> dict:
    timeout = min(120.0, _remaining(deadline, clock))
    document = verify(
        config.expected_manifest,
        config.identity_dir,
        base_url,
        timeout=timeout,
    )
    if not isinstance(document, dict) or document.get("verified") is not True:
        raise WindowError("VERIFY_FAILED", "restored state was not verified")
    return {
        "verified": True,
        "run_count": document.get("run_count"),
        "revision_count": document.get("revision_count"),
        "audit_event_count": document.get("audit_event_count"),
        "readiness": document.get("readiness"),
        "tokens_recorded": False,
    }


def _restart_service(config: RestoreConfig, **kwargs) -> None:
    command_id = _ssm_send(
        config,
        stage="restore-service-restart",
        comment="recreate application service",
        commands=["set -e", f"{SERVICE_CONTROL} remove", f"{SERVICE_CONTROL} start"],
        deadline=kwargs["deadline"],
        clock=kwargs["clock"],
        runner=kwargs["runner"],
    )
    _ssm_poll(
        config,
        stage="restore-service-restart",
        command_id=command_id,
        deadline=kwargs["deadline"],
        clock=kwargs["clock"],
        runner=kwargs["runner"],
        sleep=kwargs["sleep"],
    )


def _rollback(config: RestoreConfig, state: RestoreState, **kwargs) -> None:
    state.rollback_attempted = True
    command_id = _ssm_send(
        config,
        stage="restore-rollback",
        comment="rollback to previous data",
        commands=[f"{RESTORE_SWITCH} rollback '{config.release_id}'"],
        deadline=kwargs["deadline"],
        clock=kwargs["clock"],
        runner=kwargs["runner"],
    )
    output = _ssm_poll(
        config,
        stage="restore-rollback",
        command_id=command_id,
        deadline=kwargs["deadline"],
        clock=kwargs["clock"],
        runner=kwargs["runner"],
        sleep=kwargs["sleep"],
    )
    if "restore-switch: rolled back" not in output:
        raise WindowError("ROLLBACK_FAILED", "restore-switch did not report rollback")

    # Read logical state only at a quiesced point. Do not compare live primary
    # file bytes: SQLite may checkpoint WAL differently around restart.
    _service_action(
        config,
        "stop",
        stage="restore-rollback-stop",
        deadline=kwargs["deadline"],
        clock=kwargs["clock"],
        runner=kwargs["runner"],
        sleep=kwargs["sleep"],
        confirm_stopped=True,
    )
    logical = _logical_state(
        config,
        stage="restore-rollback-state",
        deadline=kwargs["deadline"],
        clock=kwargs["clock"],
        runner=kwargs["runner"],
        sleep=kwargs["sleep"],
    )
    if state.previous_logical_state is None or logical != state.previous_logical_state:
        raise WindowError(
            "ROLLBACK_VERIFY_FAILED",
            "logical run/revision/audit state after rollback does not match the "
            "quiesced pre-switch baseline",
        )
    _service_action(
        config,
        "start",
        stage="restore-rollback-start",
        deadline=kwargs["deadline"],
        clock=kwargs["clock"],
        runner=kwargs["runner"],
        sleep=kwargs["sleep"],
    )
    _wait(
        kwargs["ready_check"],
        deadline=kwargs["deadline"],
        clock=kwargs["clock"],
        sleep=kwargs["sleep"],
        description="previous data ready after rollback",
    )
    state.rollback_ok = True


def execute(
    config: RestoreConfig,
    *,
    runner: Callable = _run_process,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    which: Callable[[str], str | None] = shutil.which,
    process_factory: Callable[[list[str]], object] | None = None,
    port_ready: Callable[[int], bool] = _default_port_ready,
    ready_check: Callable[[str], bool] = _default_ready,
    verify: Callable[[Path, Path, str], dict] | None = None,
    terminate_process: Callable[[object], None] = _terminate_process,
) -> dict:
    verify = verify or (
        lambda manifest, identities, base_url, *, timeout: _default_verify(
            manifest, identities, base_url, timeout
        )
    )
    start = clock()
    hard_deadline = start + config.deadline_seconds
    operation_deadline = hard_deadline - config.stop_reserve_seconds
    preflight: dict[str, Any] | None = None
    state = RestoreState()
    error: WindowError | None = None
    start_attempted = False
    instance_started = False
    stopped = False
    public_ipv4 = None
    port_forward = None
    base_url = f"http://127.0.0.1:{config.local_port}"
    process_factory = process_factory or (lambda command: subprocess.Popen(command))
    try:
        if not which("session-manager-plugin"):
            raise WindowError(
                "PREREQUISITE_MISSING",
                "session-manager-plugin is required; instance was not started",
            )
        preflight = local_preflight(config)

        state.failure_stage = "precheck"
        current_state, _ = _instance_state(
            config,
            deadline=operation_deadline,
            clock=clock,
            runner=runner,
            stage="restore-precheck",
        )
        if current_state != "stopped":
            raise WindowError(
                "UNEXPECTED_STATE",
                f"instance must start stopped, observed {current_state!r}",
            )

        state.failure_stage = "start"
        start_attempted = True
        _run_stage(
            config,
            "restore-start",
            [
                "aws",
                "ec2",
                "start-instances",
                "--region",
                config.region,
                "--instance-ids",
                config.instance_id,
            ],
            deadline=operation_deadline,
            clock=clock,
            runner=runner,
        )
        instance_started = True

        def running() -> bool:
            observed, _ = _instance_state(
                config,
                deadline=operation_deadline,
                clock=clock,
                runner=runner,
                stage="restore-wait-running",
            )
            return observed == "running"

        _wait(
            running,
            deadline=operation_deadline,
            clock=clock,
            sleep=sleep,
            description="instance running",
        )

        def ssm_online() -> bool:
            output = _run_stage(
                config,
                "restore-wait-ssm",
                [
                    "aws",
                    "ssm",
                    "describe-instance-information",
                    "--region",
                    config.region,
                    "--filters",
                    f"Key=InstanceIds,Values={config.instance_id}",
                    "--query",
                    "InstanceInformationList[0].PingStatus",
                    "--output",
                    "text",
                ],
                deadline=operation_deadline,
                clock=clock,
                runner=runner,
            )
            return output.strip() == "Online"

        _wait(
            ssm_online,
            deadline=operation_deadline,
            clock=clock,
            sleep=sleep,
            description="SSM Online",
        )

        state.failure_stage = "readiness"
        readiness_id = _ssm_send(
            config,
            stage="restore-readiness",
            comment="readiness and data mount",
            commands=[
                "set -e",
                "curl -fsS http://127.0.0.1:8000/readyz",
                f"{SERVICE_CONTROL} verify",
            ],
            deadline=operation_deadline,
            clock=clock,
            runner=runner,
        )
        _ssm_poll(
            config,
            stage="restore-readiness",
            command_id=readiness_id,
            deadline=operation_deadline,
            clock=clock,
            runner=runner,
            sleep=sleep,
        )

        state.failure_stage = "destination-preflight"
        _check_destinations(
            config,
            deadline=operation_deadline,
            clock=clock,
            runner=runner,
            sleep=sleep,
        )
        state.destinations_checked = True

        state.failure_stage = "upload"
        _upload_archive(
            config,
            deadline=operation_deadline,
            clock=clock,
            runner=runner,
            sleep=sleep,
        )

        state.failure_stage = "restore"
        restore_id = _ssm_send(
            config,
            stage="restore-restore",
            comment="import and restore verified archive",
            commands=_restore_commands(config),
            deadline=operation_deadline,
            clock=clock,
            runner=runner,
        )
        restore_output = _ssm_poll(
            config,
            stage="restore-restore",
            command_id=restore_id,
            deadline=operation_deadline,
            clock=clock,
            runner=runner,
            sleep=sleep,
        )
        _require_restore_output(restore_output)

        state.failure_stage = "quiesce"
        _service_action(
            config,
            "stop",
            stage="restore-quiesce",
            deadline=operation_deadline,
            clock=clock,
            runner=runner,
            sleep=sleep,
            confirm_stopped=True,
        )
        state.failure_stage = "baseline"
        state.previous_logical_state = _logical_state(
            config,
            stage="restore-pre-switch-state",
            deadline=operation_deadline,
            clock=clock,
            runner=runner,
            sleep=sleep,
        )

        state.failure_stage = "switch"
        _switch(
            config,
            state,
            deadline=operation_deadline,
            clock=clock,
            runner=runner,
            sleep=sleep,
        )

        state.failure_stage = "verify"
        port_forward = process_factory(
            _stage_command(
                config,
                "restore-port-forward",
                [
                    "aws",
                    "ssm",
                    "start-session",
                    "--region",
                    config.region,
                    "--target",
                    config.instance_id,
                    "--document-name",
                    "AWS-StartPortForwardingSession",
                    "--parameters",
                    json.dumps(
                        {
                            "portNumber": ["8000"],
                            "localPortNumber": [str(config.local_port)],
                        }
                    ),
                ],
            )
        )
        _wait(
            lambda: port_ready(config.local_port),
            deadline=operation_deadline,
            clock=clock,
            sleep=sleep,
            description="local restore port forward",
        )
        _wait(
            lambda: ready_check(base_url),
            deadline=operation_deadline,
            clock=clock,
            sleep=sleep,
            description="restored application ready",
        )
        state.verification = _verify_state(
            config,
            base_url=base_url,
            verify=verify,
            deadline=operation_deadline,
            clock=clock,
        )
        state.restore_verified = True

        state.failure_stage = "restart"
        _restart_service(
            config,
            deadline=operation_deadline,
            clock=clock,
            runner=runner,
            sleep=sleep,
        )
        try:
            _wait(
                lambda: ready_check(base_url),
                deadline=operation_deadline,
                clock=clock,
                sleep=sleep,
                description="restarted application ready",
            )
        except WindowError as exc:
            raise WindowError(
                "SERVICE_RESTART_FAILED", f"service did not become ready: {exc}"
            ) from exc
        state.verification = _verify_state(
            config,
            base_url=base_url,
            verify=verify,
            deadline=operation_deadline,
            clock=clock,
        )
        state.service_restarted = True

        if config.rollback:
            state.failure_stage = "rollback"
            try:
                _rollback(
                    config,
                    state,
                    deadline=operation_deadline,
                    clock=clock,
                    runner=runner,
                    sleep=sleep,
                    ready_check=lambda: ready_check(base_url),
                )
            except Exception:
                state.rollback_ok = False
                raise
        state.failure_stage = None
    except WindowError as exc:
        error = exc
    except Exception as exc:  # noqa: BLE001 - converted to truthful result
        error = WindowError("UNEXPECTED_FAILURE", str(exc))
    finally:
        # The absolute deadline is defined once above. Cleanup may use only the
        # explicit small emergency allowance for stop dispatch/confirmation.
        stop_deadline = max(
            hard_deadline, clock() + min(60.0, config.stop_reserve_seconds)
        )
        if error is not None and not state.rollback_attempted:
            if state.switch_uncertain and not state.switch_completed:
                probe_deadline = min(operation_deadline, clock() + 30.0)
                if clock() >= probe_deadline:
                    state.cleanup_errors.append(
                        "switch reconciliation skipped: no remaining independent allowance"
                    )
                else:
                    try:
                        observed = _switch_state(
                            config,
                            deadline=probe_deadline,
                            clock=clock,
                            runner=runner,
                            sleep=sleep,
                        )
                    except Exception as exc:  # noqa: BLE001 - unresolved is truthful
                        state.cleanup_errors.append(
                            f"switch reconciliation failed; current/versions preserved: {exc}"
                        )
                    else:
                        state.switch_state = observed
                        if observed in {"activated", "previous_retained"}:
                            state.switch_completed = True
                        elif observed == "not_activated":
                            state.switch_uncertain = False

            if state.switch_completed:
                rollback_deadline = min(operation_deadline, clock() + 90.0)
                if clock() >= rollback_deadline:
                    state.cleanup_errors.append(
                        "automatic rollback skipped: no remaining independent allowance"
                    )
                    state.rollback_ok = False
                else:
                    try:
                        _rollback(
                            config,
                            state,
                            deadline=rollback_deadline,
                            clock=clock,
                            runner=runner,
                            sleep=sleep,
                            ready_check=lambda: ready_check(base_url),
                        )
                    except Exception as exc:  # noqa: BLE001 - preserve stop priority
                        state.rollback_ok = False
                        state.cleanup_errors.append(f"automatic rollback failed: {exc}")
                    stop_deadline = max(
                        stop_deadline,
                        clock() + min(60.0, config.stop_reserve_seconds),
                    )

        if start_attempted:
            observed_state = "unknown"
            public_ipv4 = None
            try:
                observed_state, public_ipv4 = _instance_state(
                    config,
                    deadline=stop_deadline,
                    clock=clock,
                    runner=runner,
                    stage="restore-start-reconcile",
                )
            except Exception as exc:  # noqa: BLE001
                state.cleanup_errors.append(
                    f"start outcome reconciliation failed: {exc}"
                )

            if observed_state == "stopped" and not public_ipv4:
                stopped = True
            else:
                try:
                    _run_stage(
                        config,
                        "restore-stop",
                        [
                            "aws",
                            "ec2",
                            "stop-instances",
                            "--region",
                            config.region,
                            "--instance-ids",
                            config.instance_id,
                        ],
                        deadline=stop_deadline,
                        clock=clock,
                        runner=runner,
                    )

                    def stopped_now() -> bool:
                        current, ip = _instance_state(
                            config,
                            deadline=stop_deadline,
                            clock=clock,
                            runner=runner,
                            stage="restore-stop-confirm",
                        )
                        return current == "stopped" and not ip

                    _wait(
                        stopped_now,
                        deadline=stop_deadline,
                        clock=clock,
                        sleep=sleep,
                        description="instance stopped with no public IPv4",
                    )
                    observed_state, public_ipv4 = _instance_state(
                        config,
                        deadline=stop_deadline,
                        clock=clock,
                        runner=runner,
                        stage="restore-stop-final",
                    )
                    stopped = observed_state == "stopped" and not public_ipv4
                    if not stopped:
                        state.cleanup_errors.append(
                            "instance stop confirmation failed: "
                            f"state={observed_state}, public_ip={public_ipv4}"
                        )
                except Exception as exc:  # noqa: BLE001
                    state.cleanup_errors.append(
                        "instance stop failed; operator credentials may have expired; "
                        f"no default/Admin fallback: {exc}"
                    )

        try:
            terminate_process(port_forward)
        except Exception as exc:  # noqa: BLE001
            state.cleanup_errors.append(f"port-forward cleanup failed: {exc}")

    recovery_accepted = (
        error is None
        and state.restore_verified
        and state.service_restarted
        and stopped
        and (state.rollback_ok is True if config.rollback else True)
    )
    return {
        "schema": "delivery-recovery-ec2-restore-window-v1",
        "run_id": config.run_id,
        "release_id": config.release_id,
        "preflight": preflight,
        "restore_verified": state.restore_verified,
        "service_restarted": state.service_restarted,
        "destinations_checked": state.destinations_checked,
        "switch_attempted": state.switch_attempted,
        "switch_completed": state.switch_completed,
        "switch_state": state.switch_state,
        "switch_uncertain": state.switch_uncertain,
        "baseline_state": None
        if state.previous_logical_state is None
        else _state_summary(state.previous_logical_state),
        "rollback_requested": config.rollback,
        "rollback_attempted": state.rollback_attempted,
        "rollback_ok": state.rollback_ok,
        "recovery_accepted": recovery_accepted,
        "instance_started": instance_started,
        "instance_stopped": stopped,
        "public_ipv4": public_ipv4,
        "verification": state.verification,
        "failure_stage": state.failure_stage,
        "cleanup_errors": state.cleanup_errors,
        "elapsed_seconds": round(clock() - start, 3),
        "deadline_overrun_seconds": round(
            max(0.0, (clock() - start) - config.deadline_seconds), 3
        ),
        "error": None
        if error is None
        else {"code": error.code, "message": error.message},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--expected-manifest", required=True, type=Path)
    parser.add_argument("--identity-dir", required=True, type=Path)
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--release-id")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--local-port", type=int, default=18081)
    parser.add_argument(
        "--deadline-seconds", type=int, default=DEFAULT_DEADLINE_SECONDS
    )
    parser.add_argument(
        "--stop-reserve-seconds", type=int, default=DEFAULT_STOP_RESERVE_SECONDS
    )
    args = parser.parse_args(argv)
    config = RestoreConfig(
        account_id=args.account_id,
        instance_id=args.instance_id,
        run_id=args.run_id,
        archive_path=args.archive,
        archive_sha256=args.archive_sha256,
        expected_manifest=args.expected_manifest,
        identity_dir=args.identity_dir,
        image_ref=args.image_ref,
        release_id=args.release_id or args.run_id,
        rollback=args.rollback,
        local_port=args.local_port,
        deadline_seconds=args.deadline_seconds,
        stop_reserve_seconds=args.stop_reserve_seconds,
    )
    try:
        result = execute(config)
    except WindowError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    if (
        result["recovery_accepted"]
        and result["instance_stopped"]
        and not result["cleanup_errors"]
    ):
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
