"""Local mocked tests for the bounded EC2 restore-window orchestrator."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import phase4_restore_window as window  # noqa: E402

ARCHIVE = b"synthetic-restore-archive"
ARCHIVE_HASH = hashlib.sha256(ARCHIVE).hexdigest()
IMAGE_REF = (
    "123456789012.dkr.ecr.eu-west-2.amazonaws.com/"
    "focused-delivery-recovery@sha256:" + "c" * 64
)
BASELINE_STATE = {
    "runs": [
        {
            "run_id": "run-restore",
            "status": "completed",
            "revision": 1,
            "decision_status": "approved",
        }
    ],
    "run_count": 1,
    "revision_count": 1,
    "audit_event_count": 4,
    "integrity": "ok",
}


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeProcess:
    pid = 4321

    def poll(self):
        return None

    def terminate(self):
        return None

    def wait(self, timeout=None):
        return 0

    def kill(self):
        return None


class FakeRunner:
    """Stateful fake for SSM, data switching and service state."""

    def __init__(
        self,
        *,
        fail_stage: str | None = None,
        fail_after_start: bool = False,
        reject_ssm_after_stop: bool = False,
        stop_stays_running: bool = False,
        upload_hash: str | None = None,
        restore_output: str | None = None,
        switch_mode: str | None = None,
        switch_state_fail: bool = False,
        switch_state_ambiguous: bool = False,
        destination_conflict: str | None = None,
        baseline_invalid: bool = False,
        rollback_state_mismatch: bool = False,
    ):
        self.fail_stage = fail_stage
        self.fail_after_start = fail_after_start
        self.reject_ssm_after_stop = reject_ssm_after_stop
        self.stop_stays_running = stop_stays_running
        self.upload_hash = upload_hash or ARCHIVE_HASH
        self.restore_output = restore_output
        self.switch_mode = switch_mode
        self.switch_state_fail = switch_state_fail
        self.switch_state_ambiguous = switch_state_ambiguous
        self.destination_conflict = destination_conflict
        self.baseline_invalid = baseline_invalid
        self.rollback_state_mismatch = rollback_state_mismatch
        self.calls: list[list[str]] = []
        self.stages: list[str] = []
        self.command_stages: dict[str, str] = {}
        self.next_command = 0
        self.started = False
        self.stopped = False
        self.service_running = False
        self.switch_phase = "before"
        self.ssm_after_stop = 0

    @staticmethod
    def _stage(command: list[str]) -> str:
        return command[command.index("--stage") + 1]

    def _failed(self, stage: str) -> bool:
        return self.fail_stage is not None and (
            stage == self.fail_stage or stage.startswith(self.fail_stage + "-")
        )

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        stage = self._stage(command)
        self.stages.append(stage)
        if self.fail_after_start and stage not in {
            "restore-precheck",
            "restore-start",
        }:
            return Result(1, "", "synthetic credential expiry")
        if self._failed(stage):
            return Result(1, "", "synthetic stage failure")
        if self.stopped and (
            "send-command" in command or "get-command-invocation" in command
        ):
            self.ssm_after_stop += 1
            if self.reject_ssm_after_stop:
                return Result(1, "", "InvalidInstanceId: target is stopped")
        if "describe-instances" in command:
            if stage == "restore-precheck":
                return Result(0, "stopped\tNone\n")
            if stage == "restore-wait-running":
                return Result(
                    0,
                    "running\t203.0.113.10\n" if self.started else "stopped\tNone\n",
                )
            if stage.startswith("restore-stop"):
                if self.stop_stays_running:
                    return Result(0, "running\t203.0.113.10\n")
                self.started = False
                self.stopped = True
                return Result(0, "stopped\tNone\n")
            return Result(
                0, "running\t203.0.113.10\n" if self.started else "stopped\tNone\n"
            )
        if "describe-instance-information" in command:
            return Result(0, "Online\n")
        if "start-instances" in command:
            self.started = True
            return Result(0, "starting\n")
        if "send-command" in command:
            if stage == "restore-switch":
                if self.switch_mode == "send_fail":
                    return Result(1, "", "synthetic switch send failure")
                if self.switch_mode == "partial":
                    self.switch_phase = "partial"
                    self.service_running = False
                else:
                    self.switch_phase = "activated"
                    self.service_running = True
            elif stage == "restore-quiesce":
                self.service_running = False
            elif stage == "restore-service-restart":
                self.service_running = True
            elif stage == "restore-rollback":
                self.switch_phase = "rolled_back"
                self.service_running = True
            elif stage == "restore-rollback-stop":
                self.service_running = False
            elif stage == "restore-rollback-start":
                self.service_running = True
            if stage == "restore-switch-state" and self.switch_state_fail:
                return Result(
                    1, "", "synthetic reconciliation credential failure"
                )
            self.next_command += 1
            command_id = f"cmd-{self.next_command}"
            self.command_stages[command_id] = stage
            return Result(0, f"{command_id}\n")
        if "get-command-invocation" in command:
            command_id = command[command.index("--command-id") + 1]
            invoked = self.command_stages.get(command_id, stage)
            if "Status" in command:
                return Result(0, "Success\n")
            if invoked == "restore-switch" and self.switch_mode in {
                "response_lost",
                "partial",
            }:
                return Result(1, "", "synthetic response lost after activation")
            return Result(0, self._output(invoked))
        return Result(0)

    def _logical_state(self, *, after_rollback: bool = False) -> str:
        state = json.loads(json.dumps(BASELINE_STATE))
        if after_rollback and self.rollback_state_mismatch:
            state["audit_event_count"] = 5
        return json.dumps(state, sort_keys=True) + "\n"

    def _switch_state(self) -> str:
        if self.switch_state_ambiguous:
            return "uncertain\n"
        if self.switch_phase == "activated":
            return "activated\n"
        if self.switch_phase == "partial":
            return "previous_retained\n"
        return "not_activated\n"

    def _output(self, stage: str) -> str:
        if stage == "restore-readiness":
            return "ok\n"
        if stage == "restore-destination-preflight":
            if self.destination_conflict == "incoming":
                return "/srv/delivery-recovery/incoming/run-restore.tgz\n"
            if self.destination_conflict == "restore":
                return "/srv/delivery-recovery/data/restore/run-restore\n"
            if self.destination_conflict == "versions":
                return "/srv/delivery-recovery/data/versions/run-restore\n"
            return ""
        if stage == "restore-upload":
            return f"{self.upload_hash}  /srv/delivery-recovery/incoming/run.tgz\n"
        if stage == "restore-restore":
            return self.restore_output or (
                '{"schema": "delivery-recovery-import-v1"}\n'
                '{"schema": "delivery-recovery-restore-v1", "integrity": "ok"}\n'
            )
        if stage == "restore-switch":
            return "restore-switch: activated current from release run\n"
        if stage == "restore-switch-state":
            return self._switch_state()
        if stage == "restore-pre-switch-state":
            if self.baseline_invalid:
                return '{"run_count": 1}\n'
            return self._logical_state()
        if stage == "restore-rollback-state":
            return self._logical_state(after_rollback=True)
        if stage == "restore-rollback":
            return "restore-switch: rolled back to run\n"
        return "ok\n"


class AdvancingClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _config(tmp_path: Path, **overrides) -> window.RestoreConfig:
    archive = tmp_path / "archive.tgz"
    archive.write_bytes(ARCHIVE)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": "delivery-recovery-backup-v1",
                "files": {
                    "application.db": "e" * 64,
                    "checkpoints.db": "f" * 64,
                },
                "runs": BASELINE_STATE["runs"],
                "run_count": 1,
                "revision_count": 1,
                "audit_event_count": 4,
            }
        ),
        encoding="utf-8",
    )
    identities = tmp_path / "identities"
    identities.mkdir()
    (identities / "demo-identities.json").write_text(
        json.dumps(
            {
                "identities": [
                    {"actor_id": "planner.demo", "token": "planner-token"},
                    {"actor_id": "manager.demo", "token": "manager-token"},
                ]
            }
        ),
        encoding="utf-8",
    )
    values = dict(
        account_id="123456789012",
        instance_id="i-0123456789abcdef0",
        run_id="run-restore",
        archive_path=archive,
        archive_sha256=ARCHIVE_HASH,
        expected_manifest=manifest,
        identity_dir=identities,
        image_ref=IMAGE_REF,
        release_id="run-restore",
    )
    values.update(overrides)
    return window.RestoreConfig(**values)


def _execute(
    tmp_path,
    runner,
    *,
    config=None,
    verify=None,
    clock=None,
    ready_check=lambda url: True,
    terminate_process=lambda process: None,
):
    clock = clock or (lambda: 0.0)
    verifications: list[tuple] = []

    def default_verify(manifest, identities, base_url, *, timeout=None):
        verifications.append((manifest, identities, base_url, timeout))
        return {
            "verified": True,
            "run_count": 1,
            "revision_count": 1,
            "audit_event_count": 4,
            "readiness": {"status": "ready"},
        }

    verify = verify or default_verify
    result = window.execute(
        config or _config(tmp_path),
        runner=runner,
        clock=clock,
        sleep=getattr(clock, "sleep", lambda _: None),
        which=lambda _: "session-manager-plugin",
        process_factory=lambda command: FakeProcess(),
        port_ready=lambda port: True,
        ready_check=ready_check,
        verify=verify,
        terminate_process=terminate_process,
    )
    return result, runner, verifications


def _has_stop(calls) -> bool:
    return any("stop-instances" in call for call in calls)


def _has_stage(calls, stage: str) -> bool:
    return any(
        "--stage" in call and call[call.index("--stage") + 1] == stage
        for call in calls
    )


def _uses_default_or_admin(calls) -> bool:
    joined = [" ".join(call) for call in calls]
    return any("--profile default" in line or "profile=default" in line for line in joined)


def test_restore_window_succeeds_and_stops_without_post_stop_ssm(tmp_path):
    runner = FakeRunner(reject_ssm_after_stop=True)
    terminated: list[object] = []
    result, _, verifications = _execute(
        tmp_path, runner, terminate_process=terminated.append
    )

    assert result["recovery_accepted"] is True
    assert result["restore_verified"] is True
    assert result["service_restarted"] is True
    assert result["destinations_checked"] is True
    assert result["switch_attempted"] is True
    assert result["switch_completed"] is True
    assert result["switch_state"] == "activated"
    assert result["switch_uncertain"] is False
    assert result["baseline_state"] == {
        "run_count": 1,
        "revision_count": 1,
        "audit_event_count": 4,
        "integrity": "ok",
    }
    assert result["rollback_requested"] is False
    assert result["rollback_ok"] is None
    assert result["instance_stopped"] is True
    assert result["public_ipv4"] is False
    assert result["cleanup_errors"] == []
    assert result["error"] is None
    assert result["failure_stage"] is None
    assert len(verifications) == 2
    assert _has_stop(runner.calls)
    assert runner.stopped is True
    assert runner.ssm_after_stop == 0
    assert _has_stage(runner.calls, "restore-quiesce")
    assert _has_stage(runner.calls, "restore-pre-switch-state")
    assert _has_stage(runner.calls, "restore-upload")
    assert _has_stage(runner.calls, "restore-restore")
    assert _has_stage(runner.calls, "restore-switch")
    assert _has_stage(runner.calls, "restore-service-restart")
    assert terminated and terminated[0] is not None
    assert not _uses_default_or_admin(runner.calls)
    lowered = json.dumps(result).lower()
    assert "planner-token" not in lowered and "manager-token" not in lowered
    assert result["verification"]["tokens_recorded"] is False


def test_restore_window_rollback_success_is_accepted(tmp_path):
    runner = FakeRunner(reject_ssm_after_stop=True)
    result, _, verifications = _execute(
        tmp_path, runner, config=_config(tmp_path, rollback=True)
    )

    assert result["rollback_requested"] is True
    assert result["rollback_attempted"] is True
    assert result["rollback_ok"] is True
    assert result["restore_verified"] is True
    assert result["service_restarted"] is True
    assert result["recovery_accepted"] is True
    assert result["instance_stopped"] is True
    assert len(verifications) == 2
    assert _has_stage(runner.calls, "restore-rollback")
    assert _has_stage(runner.calls, "restore-rollback-stop")
    assert _has_stage(runner.calls, "restore-rollback-state")
    assert _has_stage(runner.calls, "restore-rollback-start")
    assert not any(
        "sha256sum" in " ".join(call) and "/current/application.db" in " ".join(call)
        for call in runner.calls
    )
    assert runner.ssm_after_stop == 0


def test_restore_window_upload_failure_does_not_switch(tmp_path):
    runner = FakeRunner(fail_stage="restore-upload")
    result, _, verifications = _execute(tmp_path, runner)

    assert result["error"]["code"] == "STAGE_FAILED"
    assert result["failure_stage"] == "upload"
    assert result["restore_verified"] is False
    assert result["recovery_accepted"] is False
    assert not _has_stage(runner.calls, "restore-switch")
    assert not _has_stage(runner.calls, "restore-rollback")
    assert result["instance_stopped"] is True
    assert verifications == []


def test_restore_window_upload_hash_mismatch_stops_before_restore(tmp_path):
    runner = FakeRunner(upload_hash="9" * 64)
    result, _, _ = _execute(tmp_path, runner)

    assert result["error"]["code"] == "UPLOAD_HASH_MISMATCH"
    assert result["failure_stage"] == "upload"
    assert not _has_stage(runner.calls, "restore-restore")
    assert result["instance_stopped"] is True


def test_restore_window_restore_failure_does_not_switch(tmp_path):
    runner = FakeRunner(fail_stage="restore-restore")
    result, _, _ = _execute(tmp_path, runner)

    assert result["error"]["code"] == "STAGE_FAILED"
    assert result["failure_stage"] == "restore"
    assert result["restore_verified"] is False
    assert not _has_stage(runner.calls, "restore-switch")
    assert not _has_stage(runner.calls, "restore-rollback")
    assert result["instance_stopped"] is True


def test_restore_window_incomplete_restore_output_is_rejected(tmp_path):
    runner = FakeRunner(restore_output='{"schema": "delivery-recovery-import-v1"}\n')
    result, _, _ = _execute(tmp_path, runner)

    assert result["error"]["code"] == "RESTORE_FAILED"
    assert result["failure_stage"] == "restore"
    assert not _has_stage(runner.calls, "restore-switch")
    assert result["instance_stopped"] is True


def test_restore_window_switch_send_failure_without_activation_does_not_rollback(
    tmp_path,
):
    runner = FakeRunner(switch_mode="send_fail")
    result, _, _ = _execute(tmp_path, runner)

    assert result["error"]["code"] == "SWITCH_FAILED"
    assert result["failure_stage"] == "switch"
    assert result["switch_attempted"] is True
    assert result["switch_completed"] is False
    assert result["switch_state"] == "not_activated"
    assert result["rollback_attempted"] is False
    assert result["restore_verified"] is False
    assert result["instance_stopped"] is True


def test_restore_window_switch_response_loss_reconciles_and_verifies(tmp_path):
    runner = FakeRunner(switch_mode="response_lost")
    result, _, verifications = _execute(tmp_path, runner)

    assert result["error"] is None
    assert result["recovery_accepted"] is True
    assert result["switch_attempted"] is True
    assert result["switch_completed"] is True
    assert result["switch_state"] == "activated"
    assert result["switch_uncertain"] is False
    assert result["rollback_attempted"] is False
    assert len(verifications) == 2
    assert _has_stage(runner.calls, "restore-switch-state")
    assert result["instance_stopped"] is True


def test_restore_window_partial_switch_triggers_safe_rollback(tmp_path):
    runner = FakeRunner(switch_mode="partial")
    result, _, _ = _execute(tmp_path, runner)

    assert result["error"]["code"] == "SWITCH_PARTIAL"
    assert result["failure_stage"] == "switch"
    assert result["switch_attempted"] is True
    assert result["switch_completed"] is True
    assert result["switch_state"] == "previous_retained"
    assert result["restore_verified"] is False
    assert result["recovery_accepted"] is False
    assert result["rollback_attempted"] is True
    assert result["rollback_ok"] is True
    assert result["instance_stopped"] is True


def test_restore_window_uncertain_switch_reports_unresolved(tmp_path):
    runner = FakeRunner(switch_mode="response_lost", switch_state_fail=True)
    result, _, _ = _execute(tmp_path, runner)

    assert result["error"]["code"] == "SWITCH_STATE_UNCERTAIN"
    assert result["switch_attempted"] is True
    assert result["switch_completed"] is False
    assert result["switch_uncertain"] is True
    assert result["switch_state"] == "uncertain"
    assert result["rollback_attempted"] is False
    assert result["recovery_accepted"] is False
    assert any(
        "switch reconciliation failed" in error or "could not be reconciled" in error
        for error in result["cleanup_errors"]
    ) or "could not be reconciled" in (result["error"]["message"] or "")
    assert result["instance_stopped"] is True


def test_restore_window_verification_failure_rolls_back_and_is_not_accepted(tmp_path):
    runner = FakeRunner()
    result, _, _ = _execute(
        tmp_path,
        runner,
        verify=lambda manifest, identities, base_url, *, timeout=None: {
            "verified": False
        },
    )

    assert result["error"]["code"] == "VERIFY_FAILED"
    assert result["failure_stage"] == "verify"
    assert result["restore_verified"] is False
    assert result["recovery_accepted"] is False
    assert result["rollback_attempted"] is True
    assert result["rollback_ok"] is True
    assert _has_stage(runner.calls, "restore-rollback")
    assert result["instance_stopped"] is True


def test_restore_window_restart_failure_rolls_back(tmp_path):
    runner = FakeRunner(fail_stage="restore-service-restart")
    result, _, verifications = _execute(tmp_path, runner)

    assert result["error"]["code"] == "STAGE_FAILED"
    assert result["failure_stage"] == "restart"
    assert result["restore_verified"] is True
    assert result["service_restarted"] is False
    assert result["recovery_accepted"] is False
    assert result["rollback_attempted"] is True
    assert result["rollback_ok"] is True
    assert len(verifications) == 1
    assert result["instance_stopped"] is True


def test_restore_window_second_verification_failure_rolls_back(tmp_path):
    runner = FakeRunner()
    counter = {"value": 0}

    def verify(manifest, identities, base_url, *, timeout=None):
        counter["value"] += 1
        if counter["value"] == 1:
            return {
                "verified": True,
                "run_count": 1,
                "revision_count": 1,
                "audit_event_count": 4,
                "readiness": {"status": "ready"},
            }
        return {"verified": False}

    result, _, _ = _execute(tmp_path, runner, verify=verify)

    assert result["error"]["code"] == "VERIFY_FAILED"
    assert result["restore_verified"] is True
    assert result["service_restarted"] is False
    assert result["recovery_accepted"] is False
    assert result["rollback_attempted"] is True
    assert result["rollback_ok"] is True
    assert result["instance_stopped"] is True


def test_restore_window_rollback_failure_reports_partial(tmp_path):
    runner = FakeRunner(fail_stage="restore-rollback")
    result, _, _ = _execute(
        tmp_path, runner, config=_config(tmp_path, rollback=True)
    )

    assert result["error"]["code"] == "STAGE_FAILED"
    assert result["failure_stage"] == "rollback"
    assert result["restore_verified"] is True
    assert result["service_restarted"] is True
    assert result["rollback_attempted"] is True
    assert result["rollback_ok"] is False
    assert result["recovery_accepted"] is False
    assert result["instance_stopped"] is True
    assert sum(
        1
        for call in runner.calls
        if "--stage" in call
        and call[call.index("--stage") + 1] == "restore-rollback"
    ) == 1


def test_restore_window_rollback_logical_state_mismatch_is_not_accepted(tmp_path):
    runner = FakeRunner(rollback_state_mismatch=True)
    result, _, _ = _execute(
        tmp_path, runner, config=_config(tmp_path, rollback=True)
    )

    assert result["error"]["code"] == "ROLLBACK_VERIFY_FAILED"
    assert result["rollback_ok"] is False
    assert result["recovery_accepted"] is False
    assert result["instance_stopped"] is True


def test_restore_window_credential_expiry_is_reported_without_fallback(tmp_path):
    runner = FakeRunner(fail_after_start=True)
    result, _, _ = _execute(tmp_path, runner)

    assert result["error"]["code"] == "STAGE_FAILED"
    assert result["instance_started"] is True
    assert result["instance_stopped"] is False
    assert result["recovery_accepted"] is False
    assert any(
        "no default/Admin fallback" in error for error in result["cleanup_errors"]
    )
    assert not _uses_default_or_admin(runner.calls)


def test_restore_window_preflight_failure_never_contacts_aws(tmp_path):
    runner = FakeRunner()
    config = _config(tmp_path, archive_sha256="0" * 64)
    result, _, _ = _execute(tmp_path, runner, config=config)

    assert result["error"]["code"] == "ARCHIVE_HASH_MISMATCH"
    assert runner.calls == []
    assert result["instance_started"] is False
    assert result["instance_stopped"] is False
    assert result["recovery_accepted"] is False


def test_restore_window_rejects_unqualified_local_image_reference(tmp_path):
    runner = FakeRunner()
    config = _config(tmp_path, image_ref="delivery-recovery@sha256:" + "c" * 64)
    result, _, _ = _execute(tmp_path, runner, config=config)

    assert result["error"]["code"] == "PREFLIGHT_FAILED"
    assert runner.calls == []
    assert "full ECR" in result["error"]["message"]


def test_restore_window_near_deadline_stop_never_converges_is_bounded(tmp_path):
    clock = AdvancingClock()
    runner = FakeRunner(stop_stays_running=True)
    advanced = {"value": False}

    def ready_check(url):
        if not advanced["value"]:
            advanced["value"] = True
            clock.sleep(1650)
        return True

    def stalled_verify(manifest, identities, base_url, *, timeout=None):
        clock.sleep(max(1.0, timeout or 0))
        raise window.WindowError("VERIFY_FAILED", "synthetic near-end failure")

    result, _, _ = _execute(
        tmp_path,
        runner,
        verify=stalled_verify,
        clock=clock,
        ready_check=ready_check,
    )

    assert result["error"] is not None
    assert result["recovery_accepted"] is False
    assert _has_stop(runner.calls)
    assert result["instance_stopped"] is False
    assert result["elapsed_seconds"] <= 1860
    assert result["deadline_overrun_seconds"] <= 60


def test_restore_window_overrun_verification_still_dispatches_stop(tmp_path):
    clock = AdvancingClock()
    runner = FakeRunner()

    def stalled_verify(manifest, identities, base_url, *, timeout=None):
        clock.sleep(3000)
        raise window.WindowError("VERIFY_FAILED", "synthetic stalled verification")

    result, _, _ = _execute(
        tmp_path, runner, verify=stalled_verify, clock=clock
    )

    assert result["error"]["code"] == "VERIFY_FAILED"
    assert result["recovery_accepted"] is False
    assert _has_stop(runner.calls)
    assert result["instance_stopped"] is True
    assert result["deadline_overrun_seconds"] > 0


def test_restore_window_stop_failure_is_reported_truthfully(tmp_path):
    clock = AdvancingClock()
    runner = FakeRunner(stop_stays_running=True)
    result, _, _ = _execute(tmp_path, runner, clock=clock)

    assert result["instance_stopped"] is False
    assert result["recovery_accepted"] is False
    assert any("stop" in error.lower() for error in result["cleanup_errors"])
    assert _has_stop(runner.calls)


def test_restore_window_upload_and_restart_commands_fail_fast(tmp_path):
    runner = FakeRunner()
    result, _, _ = _execute(tmp_path, runner)
    assert result["recovery_accepted"] is True

    for stage in ("restore-upload", "restore-service-restart"):
        payloads = []
        for call in runner.calls:
            if "send-command" not in call or "--parameters" not in call:
                continue
            if call[call.index("--stage") + 1] != stage:
                continue
            payloads.append(json.loads(call[call.index("--parameters") + 1]))
        assert payloads, stage
        assert all(payload["commands"][0] == "set -e" for payload in payloads), stage


@pytest.mark.parametrize("conflict", ["incoming", "restore", "versions"])
def test_restore_window_preexisting_destination_rejects_without_switch_or_rollback(
    tmp_path, conflict
):
    runner = FakeRunner(destination_conflict=conflict)
    result, _, verifications = _execute(tmp_path, runner)

    assert result["error"]["code"] == "DESTINATION_CONFLICT"
    assert result["failure_stage"] == "destination-preflight"
    assert result["destinations_checked"] is False
    assert result["switch_attempted"] is False
    assert result["switch_completed"] is False
    assert result["rollback_attempted"] is False
    assert not _has_stage(runner.calls, "restore-quiesce")
    assert not _has_stage(runner.calls, "restore-switch")
    assert not _has_stage(runner.calls, "restore-rollback")
    assert verifications == []
    assert result["instance_stopped"] is True
    assert runner.stopped is True


def test_restore_window_ambiguous_path_state_is_not_directory_presence_only(tmp_path):
    runner = FakeRunner(
        switch_mode="response_lost",
        switch_state_ambiguous=True,
    )
    result, _, _ = _execute(tmp_path, runner)

    assert result["error"]["code"] == "SWITCH_STATE_UNCERTAIN"
    assert result["destinations_checked"] is True
    assert result["switch_attempted"] is True
    assert result["switch_state"] == "uncertain"
    assert result["rollback_attempted"] is False
    assert result["recovery_accepted"] is False
    assert result["instance_stopped"] is True


def test_restore_window_logical_state_command_copies_to_writable_tmpfs():
    command = window._logical_state_command(
        "example.invalid/repo@sha256:" + "a" * 64
    )
    assert "shutil.copyfile" in command
    assert "mode=rw" in command
    assert "/tmp/restore-state/application.db" in command
    assert "application.db-wal" in command
    assert "application.db-shm" in command
    assert "immutable=1" not in command
    assert "PRAGMA integrity_check" in command
    assert "--read-only" in command
    assert "--tmpfs /tmp:size=64m,mode=1777" in command
    assert ":/data:ro" in command


def test_restore_window_baseline_failure_prevents_switch_and_stops(tmp_path):
    runner = FakeRunner(fail_stage="restore-pre-switch-state")
    result, _, verifications = _execute(tmp_path, runner)

    assert result["error"]["code"] == "STAGE_FAILED"
    assert result["failure_stage"] == "baseline"
    assert result["switch_attempted"] is False
    assert result["switch_completed"] is False
    assert result["rollback_attempted"] is False
    assert not _has_stage(runner.calls, "restore-switch")
    assert not _has_stage(runner.calls, "restore-rollback")
    assert verifications == []
    assert result["instance_stopped"] is True


def test_restore_window_invalid_baseline_prevents_switch_and_stops(tmp_path):
    runner = FakeRunner(baseline_invalid=True)
    result, _, _ = _execute(tmp_path, runner)

    assert result["error"]["code"] == "STATE_READ_FAILED"
    assert result["failure_stage"] == "baseline"
    assert result["switch_attempted"] is False
    assert not _has_stage(runner.calls, "restore-switch")
    assert not _has_stage(runner.calls, "restore-rollback")
    assert result["instance_stopped"] is True


def test_restore_window_quiesce_failure_prevents_baseline_and_switch(tmp_path):
    runner = FakeRunner(fail_stage="restore-quiesce")
    result, _, _ = _execute(tmp_path, runner)

    assert result["error"]["code"] == "STAGE_FAILED"
    assert result["failure_stage"] == "quiesce"
    assert not _has_stage(runner.calls, "restore-pre-switch-state")
    assert result["switch_attempted"] is False
    assert not _has_stage(runner.calls, "restore-switch")
    assert result["instance_stopped"] is True
