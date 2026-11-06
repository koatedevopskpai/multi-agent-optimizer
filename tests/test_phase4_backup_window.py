"""Local mocked tests for the bounded cloud backup-window orchestrator."""

from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import phase4_backup_window as window  # noqa: E402

ARCHIVE = b"synthetic-archive"
ARCHIVE_HASH = hashlib.sha256(ARCHIVE).hexdigest()


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    def __init__(
        self,
        *,
        fail_stage: str | None = None,
        fail_after_start: bool = False,
        invocation_retries: int = 0,
        reject_ssm_after_stop: bool = False,
    ):
        self.fail_stage = fail_stage
        self.fail_after_start = fail_after_start
        self.invocation_retries = invocation_retries
        self.reject_ssm_after_stop = reject_ssm_after_stop
        self.calls: list[list[str]] = []
        self.invocation_attempts = 0
        self.stopped = False
        self.ssm_after_stop = 0

    @staticmethod
    def _stage(command: list[str]) -> str:
        return command[command.index("--stage") + 1]

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        stage = self._stage(command)
        if self.fail_after_start and stage not in {"recovery-precheck", "recovery-start"}:
            return Result(1, "", "identity_lookup_failed")
        if self.fail_stage and stage.startswith(self.fail_stage):
            return Result(1, "", "synthetic stage failure")
        if self.stopped and ("send-command" in command or "get-command-invocation" in command):
            self.ssm_after_stop += 1
            if self.reject_ssm_after_stop:
                return Result(1, "", "InvalidInstanceId: target stopped")
        if "describe-instances" in command:
            if stage == "recovery-precheck":
                return Result(0, "stopped\tNone\n")
            if stage.startswith("recovery-stop"):
                self.stopped = True
                return Result(0, "stopped\tNone\n")
            return Result(0, "running\t203.0.113.10\n")
        if "describe-instance-information" in command:
            return Result(0, "Online\n")
        if "send-command" in command:
            return Result(0, f"cmd-{stage}\n")
        if "get-command-invocation" in command:
            if "Status" in command:
                if stage.startswith("recovery-readiness-status"):
                    if self.invocation_attempts < self.invocation_retries:
                        self.invocation_attempts += 1
                        return Result(1, "", "InvocationDoesNotExist")
                return Result(0, "Success\n")
            if stage.startswith("recovery-remote-hash-output"):
                return Result(0, f"{ARCHIVE_HASH}  /remote/archive.tgz\n")
            return Result(0, "ok\n")
        return Result(0)


class AmbiguousStartRunner(FakeRunner):
    def __init__(self, *, raise_timeout=False):
        super().__init__()
        self.raise_timeout = raise_timeout

    def __call__(self, command, **kwargs):
        stage = self._stage(command)
        if stage == "recovery-start":
            self.calls.append(list(command))
            if self.raise_timeout:
                raise TimeoutError("simulated accepted start with response loss")
            return Result(1, "", "simulated transport loss after acceptance")
        return super().__call__(command, **kwargs)


class FakeProcess:
    pid = 1234

    def poll(self):
        return None

    def terminate(self):
        return None

    def wait(self, timeout=None):
        return 0

    def kill(self):
        return None


class AdvancingClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _config(tmp_path: Path) -> window.WindowConfig:
    return window.WindowConfig(
        account_id="123456789012",
        instance_id="i-0123456789abcdef0",
        run_id="run-test",
        archive_path=tmp_path / "archive.tgz",
    )


def _download(url: str, destination: Path, timeout: float):
    destination.write_bytes(ARCHIVE)
    return ARCHIVE_HASH, len(ARCHIVE)


def _execute(
    tmp_path,
    runner,
    *,
    download=_download,
    clock=None,
    terminate_process=lambda p: None,
):
    clock = clock or (lambda: 0.0)
    result = window.execute(
        _config(tmp_path),
        runner=runner,
        clock=clock,
        sleep=getattr(clock, "sleep", lambda _: None),
        which=lambda _: "session-manager-plugin",
        download=download,
        process_factory=lambda command: FakeProcess(),
        port_ready=lambda port: True,
        terminate_process=terminate_process,
    )
    return result, runner.calls, []


def _has_stop(calls) -> bool:
    return any("stop-instances" in call for call in calls)


def _uses_default_or_admin(calls) -> bool:
    joined = [" ".join(call) for call in calls]
    return any("--profile default" in line or "profile=default" in line for line in joined)


def test_success_stops_compute_without_ssm_after_stop(tmp_path):
    runner = FakeRunner(reject_ssm_after_stop=True)
    result, calls, _ = _execute(tmp_path, runner)
    assert result["archive_verified"] is True
    assert result["instance_stopped"] is True
    assert result["cleanup_errors"] == []
    assert _has_stop(calls)
    assert runner.ssm_after_stop == 0
    assert not _uses_default_or_admin(calls)


@pytest.mark.parametrize("fail_stage", ["recovery-readiness", "recovery-backup"])
def test_stop_attempted_after_readiness_or_export_failure(tmp_path, fail_stage):
    runner = FakeRunner(fail_stage=fail_stage)
    result = _execute(tmp_path, runner)[0]
    assert result["archive_verified"] is False
    assert result["error"]["code"] == "STAGE_FAILED"
    assert _has_stop(runner.calls)


def test_stop_attempted_after_download_failure(tmp_path):
    def failing_download(url, destination, timeout):
        raise OSError("synthetic transfer failure")

    runner = FakeRunner()
    result = _execute(tmp_path, runner, download=failing_download)[0]
    assert result["archive_verified"] is False
    assert result["error"]["code"] == "UNEXPECTED_FAILURE"
    assert _has_stop(runner.calls)


def test_stalled_transfer_expires_but_stop_is_still_dispatched(tmp_path):
    clock = AdvancingClock()

    def stalled_download(url, destination, timeout):
        clock.sleep(2000)
        raise window.WindowError("DOWNLOAD_TIMEOUT", "synthetic stalled transfer")

    runner = FakeRunner()
    result = _execute(tmp_path, runner, download=stalled_download, clock=clock)[0]
    assert result["archive_verified"] is False
    assert result["error"]["code"] == "DOWNLOAD_TIMEOUT"
    assert _has_stop(runner.calls)
    assert result["instance_stopped"] is True
    assert result["deadline_overrun_seconds"] >= 0


def test_ambiguous_start_is_reconciled_and_stopped(tmp_path):
    for raise_timeout in (False, True):
        runner = AmbiguousStartRunner(raise_timeout=raise_timeout)
        result = _execute(tmp_path, runner)[0]
        assert result["archive_verified"] is False
        assert _has_stop(runner.calls), raise_timeout
        assert result["instance_stopped"] is True


def test_identity_preflight_failure_does_not_start_or_stop(tmp_path):
    runner = FakeRunner(fail_stage="recovery-precheck")
    result = _execute(tmp_path, runner)[0]
    assert result["archive_verified"] is False
    assert not _has_stop(runner.calls)
    assert not any("start-instances" in call for call in runner.calls)


def test_credential_expiry_reports_without_admin_fallback(tmp_path):
    runner = FakeRunner(fail_after_start=True)
    result = _execute(tmp_path, runner)[0]
    assert result["archive_verified"] is False
    assert result["instance_stopped"] is False
    assert any("no default/Admin fallback" in error for error in result["cleanup_errors"])
    assert not _uses_default_or_admin(runner.calls)


def test_invocation_does_not_exist_retries_are_bounded(tmp_path):
    runner = FakeRunner(invocation_retries=2)
    result = _execute(tmp_path, runner)[0]
    assert result["archive_verified"] is True
    assert runner.invocation_attempts == 2


def test_subprocess_timeout_terminates_tree(monkeypatch):
    terminated = []
    original = window._terminate_process

    def record(process):
        terminated.append(process)
        original(process)

    monkeypatch.setattr(window, "_terminate_process", record)
    started = time.monotonic()
    with pytest.raises(window.WindowError) as error:
        window._run_process(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=0.2,
        )
    elapsed = time.monotonic() - started
    assert error.value.code == "STAGE_TIMEOUT"
    assert terminated
    assert elapsed < 10
