"""Bounded operator-guarded cloud backup window.

This script starts the stopped development instance, runs the reviewed backup
export over SSM, downloads the archive through an SSM port-forward, verifies the
local/remote SHA-256, stops compute, and always attempts cleanup in ``finally``.

It never falls back to default/Admin credentials. Every AWS operation is invoked
through ``phase4_stage_exec.py`` with the focused-operator profile, expected
account and expected role. It is designed to be unit tested with injected
runners; the CLI is the only path that contacts AWS.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parents[1]
STAGE = REPO / "scripts" / "phase4_stage_exec.py"
PROFILE = "focused-operator"
ROLE = "focused-delivery-recovery-operator"
DEFAULT_DEADLINE_SECONDS = 1800
DEFAULT_STOP_RESERVE_SECONDS = 180


class WindowError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class WindowConfig:
    account_id: str
    instance_id: str
    run_id: str
    archive_path: Path
    local_port: int = 18080
    remote_port: int = 18080
    region: str = "eu-west-2"
    deadline_seconds: int = DEFAULT_DEADLINE_SECONDS
    stop_reserve_seconds: int = DEFAULT_STOP_RESERVE_SECONDS


def _stage_command(config: WindowConfig, stage: str, arguments: list[str]) -> list[str]:
    return [
        sys.executable,
        str(STAGE),
        "--stage",
        stage,
        "--profile",
        PROFILE,
        "--expected-account",
        config.account_id,
        "--expected-role",
        ROLE,
        "--quiet",
        "--",
        *arguments,
    ]


def _remaining(deadline: float, clock: Callable[[], float], reserve: float = 0.0) -> float:
    remaining = deadline - clock() - reserve
    if remaining <= 0:
        raise WindowError("DEADLINE_EXCEEDED", "overall backup-window deadline reached")
    return remaining


def _run_stage(
    config: WindowConfig,
    stage: str,
    arguments: list[str],
    *,
    deadline: float,
    clock: Callable[[], float],
    runner: Callable,
    reserve: float = 0.0,
) -> str:
    timeout = max(1.0, min(120.0, _remaining(deadline, clock, reserve)))
    completed = runner(
        _stage_command(config, stage, arguments),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if getattr(completed, "returncode", 1) != 0:
        stderr = (getattr(completed, "stderr", "") or "").strip()
        raise WindowError(
            "STAGE_FAILED",
            f"{stage} failed under the verified operator identity: {stderr[:400]}",
        )
    return (getattr(completed, "stdout", "") or "").strip()


def _wait(
    check: Callable[[], bool],
    *,
    deadline: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    description: str,
    interval: float = 5.0,
) -> None:
    while True:
        if check():
            return
        remaining = _remaining(deadline, clock)
        if remaining <= 1:
            raise WindowError("DEADLINE_EXCEEDED", f"timed out waiting for {description}")
        sleep(min(interval, remaining))


def _instance_state(
    config: WindowConfig,
    *,
    deadline: float,
    clock: Callable[[], float],
    runner: Callable,
    stage: str,
) -> tuple[str, bool]:
    output = _run_stage(
        config,
        stage,
        [
            "aws",
            "ec2",
            "describe-instances",
            "--region",
            config.region,
            "--instance-ids",
            config.instance_id,
            "--query",
            "Reservations[0].Instances[0].[State.Name,PublicIpAddress]",
            "--output",
            "text",
        ],
        deadline=deadline,
        clock=clock,
        runner=runner,
    )
    fields = output.split()
    state = fields[0] if fields else ""
    public_ip = bool(fields[1]) if len(fields) > 1 and fields[1] not in {"None", "null"} else False
    return state, public_ip


def _ssm_send(
    config: WindowConfig,
    *,
    stage: str,
    comment: str,
    commands: list[str],
    deadline: float,
    clock: Callable[[], float],
    runner: Callable,
) -> str:
    payload = json.dumps({"commands": commands})
    output = _run_stage(
        config,
        stage,
        [
            "aws",
            "ssm",
            "send-command",
            "--region",
            config.region,
            "--instance-ids",
            config.instance_id,
            "--document-name",
            "AWS-RunShellScript",
            "--comment",
            comment,
            "--timeout-seconds",
            "600",
            "--parameters",
            payload,
            "--query",
            "Command.CommandId",
            "--output",
            "text",
        ],
        deadline=deadline,
        clock=clock,
        runner=runner,
    )
    command_id = output.splitlines()[-1].strip() if output else ""
    if not command_id:
        raise WindowError("SSM_COMMAND_FAILED", f"{stage} returned no command id")
    return command_id


def _ssm_poll(
    config: WindowConfig,
    *,
    stage: str,
    command_id: str,
    deadline: float,
    clock: Callable[[], float],
    runner: Callable,
    sleep: Callable[[float], None],
) -> str:
    retries = 0
    while True:
        try:
            status = _run_stage(
                config,
                f"{stage}-status",
                [
                    "aws",
                    "ssm",
                    "get-command-invocation",
                    "--region",
                    config.region,
                    "--command-id",
                    command_id,
                    "--instance-id",
                    config.instance_id,
                    "--query",
                    "Status",
                    "--output",
                    "text",
                ],
                deadline=deadline,
                clock=clock,
                runner=runner,
            )
        except WindowError as exc:
            if "InvocationDoesNotExist" in exc.message and retries < 5:
                retries += 1
                sleep(min(2.0, _remaining(deadline, clock)))
                continue
            raise
        status = status.strip()
        if status == "Success":
            return _run_stage(
                config,
                f"{stage}-output",
                [
                    "aws",
                    "ssm",
                    "get-command-invocation",
                    "--region",
                    config.region,
                    "--command-id",
                    command_id,
                    "--instance-id",
                    config.instance_id,
                    "--query",
                    "StandardOutputContent",
                    "--output",
                    "text",
                ],
                deadline=deadline,
                clock=clock,
                runner=runner,
            )
        if status in {"Pending", "InProgress", "Delayed"}:
            sleep(min(3.0, _remaining(deadline, clock)))
            continue
        raise WindowError(
            "SSM_COMMAND_FAILED", f"{stage} ended with status {status}"
        )


def _default_port_ready(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _terminate_process(process) -> None:
    if process is None:
        return
    if getattr(process, "poll", lambda: 0)() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            timeout=10,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _run_process(command, **kwargs):
    """Run a child process and kill its tree on timeout."""
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=kwargs.get("timeout"))
    except subprocess.TimeoutExpired as exc:
        _terminate_process(process)
        raise WindowError(
            "STAGE_TIMEOUT", f"stage wrapper timed out: {exc}"
        ) from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _download_archive(url: str, destination: Path, timeout: float) -> tuple[str, int]:
    """Download through a tracked subprocess that can be killed at the deadline."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    code = (
        "import sys, urllib.request; "
        "urllib.request.urlretrieve(sys.argv[1], sys.argv[2])"
    )
    try:
        completed = _run_process(
            [sys.executable, "-c", code, url, str(destination)],
            timeout=max(1.0, timeout),
        )
    except WindowError as exc:
        if exc.code == "STAGE_TIMEOUT":
            raise WindowError(
                "DOWNLOAD_TIMEOUT", "transfer subprocess exceeded the operation deadline"
            ) from exc
        raise
    if completed.returncode != 0:
        raise WindowError(
            "DOWNLOAD_FAILED", (completed.stderr or "transfer subprocess failed")[:400]
        )
    data = destination.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def execute(
    config: WindowConfig,
    *,
    runner: Callable = _run_process,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    which: Callable[[str], str | None] = shutil.which,
    download: Callable[[str, Path, float], tuple[str, int]] = _download_archive,
    process_factory: Callable[[list[str]], object] | None = None,
    port_ready: Callable[[int], bool] = _default_port_ready,
    terminate_process: Callable[[object], None] = _terminate_process,
) -> dict:
    if not which("session-manager-plugin"):
        raise WindowError(
            "PREREQUISITE_MISSING",
            "session-manager-plugin is required; instance was not started",
        )
    hard_deadline = clock() + config.deadline_seconds
    operation_deadline = hard_deadline - config.stop_reserve_seconds
    start = clock()
    start_attempted = False
    port_forward = None
    archive_verified = False
    cleanup_errors: list[str] = []
    stopped = False
    public_ip = None
    error: WindowError | None = None
    process_factory = process_factory or (
        lambda command: subprocess.Popen(command)
    )
    try:
        state, _ = _instance_state(
            config,
            deadline=operation_deadline,
            clock=clock,
            runner=runner,
            stage="recovery-precheck",
        )
        if state != "stopped":
            raise WindowError(
                "UNEXPECTED_STATE", f"instance must start stopped, observed {state!r}"
            )
        start_attempted = True
        _run_stage(
            config,
            "recovery-start",
            [
                "aws", "ec2", "start-instances", "--region", config.region,
                "--instance-ids", config.instance_id,
            ],
            deadline=operation_deadline,
            clock=clock,
            runner=runner,
        )

        def running() -> bool:
            state, _ = _instance_state(
                config, deadline=operation_deadline, clock=clock,
                runner=runner, stage="recovery-wait-running",
            )
            return state == "running"

        _wait(running, deadline=operation_deadline, clock=clock, sleep=sleep,
              description="instance running")

        def ssm_online() -> bool:
            output = _run_stage(
                config,
                "recovery-wait-ssm",
                [
                    "aws", "ssm", "describe-instance-information", "--region",
                    config.region, "--filters",
                    f"Key=InstanceIds,Values={config.instance_id}", "--query",
                    "InstanceInformationList[0].PingStatus", "--output", "text",
                ],
                deadline=operation_deadline, clock=clock, runner=runner,
            )
            return output.strip() == "Online"

        _wait(ssm_online, deadline=operation_deadline, clock=clock, sleep=sleep,
              description="SSM Online")

        readiness_id = _ssm_send(
            config, stage="recovery-readiness", comment="readiness and data mount",
            commands=[
                "set -e",
                "curl -fsS http://127.0.0.1:8000/readyz",
                "/usr/local/lib/delivery-recovery/service-control.sh verify",
            ],
            deadline=operation_deadline, clock=clock, runner=runner,
        )
        _ssm_poll(config, stage="recovery-readiness", command_id=readiness_id,
                  deadline=operation_deadline, clock=clock, runner=runner, sleep=sleep)

        export_id = _ssm_send(
            config, stage="recovery-backup", comment="phase4 backup export",
            commands=[f"/usr/local/lib/delivery-recovery/backup-export.sh {config.run_id}"],
            deadline=operation_deadline, clock=clock, runner=runner,
        )
        export_output = _ssm_poll(
            config, stage="recovery-backup", command_id=export_id,
            deadline=operation_deadline, clock=clock, runner=runner, sleep=sleep,
        )
        if not export_output.strip():
            raise WindowError("EXPORT_FAILED", "backup helper produced no output")

        listener_id = _ssm_send(
            config, stage="recovery-listener", comment="backup transfer listener",
            commands=[
                "nohup timeout 900 python3 -m http.server "
                f"{config.remote_port} --bind 127.0.0.1 "
                "--directory /srv/delivery-recovery/backups "
                ">/run/backup-http.log 2>&1 & echo $! > /run/backup-http.pid",
                "sleep 1",
                f"curl -fsSI http://127.0.0.1:{config.remote_port}/{config.run_id}.tgz",
            ],
            deadline=operation_deadline, clock=clock, runner=runner,
        )
        _ssm_poll(config, stage="recovery-listener", command_id=listener_id,
                  deadline=operation_deadline, clock=clock, runner=runner, sleep=sleep)

        port_forward = process_factory(
            _stage_command(
                config,
                "recovery-port-forward",
                [
                    "aws", "ssm", "start-session", "--region", config.region,
                    "--target", config.instance_id, "--document-name",
                    "AWS-StartPortForwardingSession", "--parameters",
                    json.dumps(
                        {
                            "portNumber": [str(config.remote_port)],
                            "localPortNumber": [str(config.local_port)],
                        }
                    ),
                ],
            )
        )
        _wait(lambda: port_ready(config.local_port), deadline=operation_deadline,
              clock=clock, sleep=sleep, description="local port forward")

        url = f"http://127.0.0.1:{config.local_port}/{config.run_id}.tgz"
        local_hash, _ = download(
            url,
            config.archive_path,
            _remaining(operation_deadline, clock),
        )
        if clock() > operation_deadline:
            raise WindowError(
                "DOWNLOAD_TIMEOUT", "download exceeded the overall deadline"
            )
        remote_hash = _ssm_poll(
            config,
            stage="recovery-remote-hash",
            command_id=_ssm_send(
                config, stage="recovery-remote-hash", comment="archive hash",
                commands=[f"sha256sum /srv/delivery-recovery/backups/{config.run_id}.tgz"],
                deadline=operation_deadline, clock=clock, runner=runner,
            ),
            deadline=operation_deadline, clock=clock, runner=runner, sleep=sleep,
        )
        remote_digest = remote_hash.strip().split()[0] if remote_hash.strip() else ""
        if local_hash != remote_digest:
            raise WindowError(
                "HASH_MISMATCH",
                f"local {local_hash} != remote {remote_digest}",
            )
        archive_verified = True
    except WindowError as exc:
        error = exc
    except Exception as exc:
        error = WindowError("UNEXPECTED_FAILURE", str(exc))
    finally:
        # Stop dispatch and confirmation get priority over local cleanup. A
        # separately bounded emergency allowance ensures deadline expiry cannot
        # suppress the stop attempt.
        stop_deadline = max(
            hard_deadline, clock() + min(60.0, config.stop_reserve_seconds)
        )
        if start_attempted:
            state = "unknown"
            public_ip = None
            try:
                state, public_ip = _instance_state(
                    config, deadline=stop_deadline, clock=clock,
                    runner=runner, stage="recovery-start-reconcile",
                )
            except Exception as exc:
                cleanup_errors.append(f"start outcome reconciliation failed: {exc}")

            if state == "stopped" and not public_ip:
                stopped = True
            else:
                try:
                    _run_stage(
                        config,
                        "recovery-stop",
                        [
                            "aws", "ec2", "stop-instances", "--region", config.region,
                            "--instance-ids", config.instance_id,
                        ],
                        deadline=stop_deadline, clock=clock, runner=runner,
                    )

                    def stopped_now() -> bool:
                        current_state, current_ip = _instance_state(
                            config, deadline=stop_deadline, clock=clock,
                            runner=runner, stage="recovery-stop-confirm",
                        )
                        return current_state == "stopped" and not current_ip

                    _wait(
                        stopped_now,
                        deadline=stop_deadline, clock=clock, sleep=sleep,
                        description="instance stopped with no public IPv4",
                    )
                    state, public_ip = _instance_state(
                        config, deadline=stop_deadline, clock=clock,
                        runner=runner, stage="recovery-stop-final",
                    )
                    stopped = state == "stopped" and not public_ip
                    if not stopped:
                        cleanup_errors.append(
                            f"instance stop confirmation failed: state={state}, public_ip={public_ip}"
                        )
                except Exception as exc:
                    cleanup_errors.append(
                        "instance stop failed; operator credentials may have expired; "
                        f"no default/Admin fallback: {exc}"
                    )

        try:
            terminate_process(port_forward)
        except Exception as exc:
            cleanup_errors.append(f"port-forward cleanup failed: {exc}")

    return {
        "archive_verified": archive_verified,
        "instance_stopped": stopped,
        "public_ipv4": public_ip,
        "cleanup_errors": cleanup_errors,
        "elapsed_seconds": round(clock() - start, 3),
        "deadline_overrun_seconds": round(
            max(0.0, (clock() - start) - config.deadline_seconds), 3
        ),
        "error": None if error is None else {"code": error.code, "message": error.message},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--local-port", type=int, default=18080)
    parser.add_argument("--remote-port", type=int, default=18080)
    parser.add_argument("--deadline-seconds", type=int, default=DEFAULT_DEADLINE_SECONDS)
    args = parser.parse_args(argv)
    config = WindowConfig(
        account_id=args.account_id,
        instance_id=args.instance_id,
        run_id=args.run_id,
        archive_path=args.archive,
        local_port=args.local_port,
        remote_port=args.remote_port,
        deadline_seconds=args.deadline_seconds,
    )
    try:
        result = execute(config)
    except WindowError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["archive_verified"] and result["instance_stopped"] and not result["cleanup_errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
