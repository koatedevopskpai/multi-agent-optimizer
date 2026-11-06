"""Integration tests for the boto3 MFA bootstrap executor and shared manifest."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import phase4_iam_manifest as manifest_builder  # noqa: E402
import phase4_initial_bootstrap as bootstrap  # noqa: E402
import phase4_review_receipt as receipt_module  # noqa: E402

ACCOUNT = "123456789012"
PRINCIPAL = f"arn:aws:iam::{ACCOUNT}:user/koate"
MFA_SERIAL = f"arn:aws:iam::{ACCOUNT}:mfa/demo-user"
SESSION_SECRET = "session-secret"
SESSION_TOKEN = "session-token"


class NoSuchEntityError(Exception):
    response = {"Error": {"Code": "NoSuchEntity"}}


class FakeSts:
    def __init__(self, expiration: datetime, account: str = ACCOUNT, arn: str = PRINCIPAL) -> None:
        self.expiration = expiration
        self.account = account
        self.arn = arn
        self.calls: list[str] = []

    def get_caller_identity(self):
        self.calls.append("get_caller_identity")
        return {"Account": self.account, "Arn": self.arn}

    def get_session_token(self, **kwargs):
        self.calls.append("get_session_token")
        assert kwargs["DurationSeconds"] == bootstrap.DURATION_SECONDS
        self.last_credentials = {
            "AccessKeyId": "ASIAEXAMPLE",
            "SecretAccessKey": SESSION_SECRET,
            "SessionToken": SESSION_TOKEN,
            "Expiration": self.expiration,
        }
        return {"Credentials": self.last_credentials}


class FakeIam:
    def __init__(self, role_exists: bool = False, put_error: bool = False) -> None:
        self.role_exists = role_exists
        self.put_error = put_error
        self.calls: list[tuple] = []

    def get_role(self, **kwargs):
        self.calls.append(("get_role", kwargs))
        if self.role_exists:
            return {"Role": {"RoleName": kwargs["RoleName"]}}
        raise NoSuchEntityError()

    def create_role(self, **kwargs):
        self.calls.append(("create_role", kwargs))
        return {}

    def put_role_policy(self, **kwargs):
        self.calls.append(("put_role_policy", kwargs))
        if self.put_error:
            raise RuntimeError("put failed")
        return {}

    def delete_role(self, **kwargs):
        self.calls.append(("delete_role", kwargs))
        return {}


class FakeSession:
    def __init__(self, sts: FakeSts, iam: FakeIam) -> None:
        self.sts = sts
        self.iam = iam

    def client(self, service, **kwargs):
        if service == "sts":
            return self.sts
        if service == "iam":
            return self.iam
        raise AssertionError(service)


def _fixture_documents(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    rendered = tmp_path / "iam"
    rendered.mkdir()
    names = list(manifest_builder.ACTIVE)
    for name in names:
        (rendered / name).write_bytes(b'{"Version":"2012-10-17","Statement":[]}')
    # Make the two bootstrap inputs distinguishable for byte-submission tests.
    (rendered / bootstrap.TRUST_DOC).write_bytes(b'{"trust":true}')
    (rendered / bootstrap.POLICY_DOC).write_bytes(b'{"policy":true}')
    terraform = tmp_path / "main.tf"
    terraform.write_bytes(b"terraform source")
    document = manifest_builder.build_manifest(rendered, terraform)
    manifest = rendered / "active-manifest.json"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    receipt = tmp_path / "independent-review-receipt.json"
    receipt.write_text(
        json.dumps({
            "schema": receipt_module.SCHEMA,
            "reviewer": "synthetic-test-codex",
            "reviewed_at_utc": "2026-11-04T00:00:00Z",
            "decision": "accepted",
            "scope": receipt_module.SCOPE,
            "manifest_schema": document["schema"],
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "active_document_count": document["active_document_count"],
            "terraform_source_sha256": document["terraform_generated_source"]["sha256"],
        }),
        encoding="utf-8",
    )
    return rendered, manifest, terraform, receipt


def _run(rendered: Path, manifest: Path, receipt: Path, iam: FakeIam, **overrides):
    sts = overrides.pop("sts", FakeSts(datetime.now(timezone.utc) + timedelta(minutes=15)))
    session = FakeSession(sts, iam)
    documents = bootstrap.prepare_reviewed_inputs(manifest, rendered, receipt)
    values = {
        "source_session": session,
        "expected_account": ACCOUNT,
        "expected_principal": PRINCIPAL,
        "mfa_serial": MFA_SERIAL,
        "token_code": "123456",
        "trust_bytes": documents[bootstrap.TRUST_DOC],
        "policy_bytes": documents[bootstrap.POLICY_DOC],
        "session_factory": lambda **kwargs: session,
        "now": datetime.now(timezone.utc),
    }
    values.update(overrides)
    return bootstrap.run_initial_bootstrap(**values)


def test_production_builder_and_verifier_use_the_same_schema(tmp_path):
    rendered, manifest, _, receipt = _fixture_documents(tmp_path)
    documents = bootstrap.prepare_reviewed_inputs(manifest, rendered, receipt)
    assert documents[bootstrap.TRUST_DOC] == b'{"trust":true}'
    assert documents[bootstrap.POLICY_DOC] == b'{"policy":true}'
    parsed = json.loads(manifest.read_text(encoding="utf-8"))
    assert parsed["active_documents"][bootstrap.TRUST_DOC]["sha256"] == hashlib.sha256(
        b'{"trust":true}'
    ).hexdigest()
    assert parsed["active_documents"][bootstrap.TRUST_DOC]["bytes"] == len(b'{"trust":true}')


def test_altered_size_or_hash_is_rejected_before_any_session(tmp_path):
    rendered, manifest, _, receipt = _fixture_documents(tmp_path)
    (rendered / bootstrap.TRUST_DOC).write_bytes(b'{"changed":true}')
    with pytest.raises(Exception):
        bootstrap.prepare_reviewed_inputs(manifest, rendered, receipt)
    (rendered / bootstrap.TRUST_DOC).write_bytes(b'{"trust":true,"extra":1}')
    with pytest.raises(Exception):
        bootstrap.prepare_reviewed_inputs(manifest, rendered, receipt)


def test_malformed_manifest_is_rejected():
    with pytest.raises(Exception):
        bootstrap.prepare_reviewed_inputs(Path("missing.json"), Path("missing"), Path("missing-receipt.json"))


def test_successful_short_mfa_session_submits_verified_bytes(tmp_path):
    rendered, manifest, _, receipt = _fixture_documents(tmp_path)
    iam = FakeIam()
    result = _run(rendered, manifest, receipt, iam)
    assert result["status"] == "bootstrap_role_created"
    assert result["duration_seconds"] == 900
    create = next(call for call in iam.calls if call[0] == "create_role")
    put = next(call for call in iam.calls if call[0] == "put_role_policy")
    assert create[1]["AssumeRolePolicyDocument"] == '{"trust":true}'
    assert put[1]["PolicyDocument"] == '{"policy":true}'


def test_unexpectedly_long_session_is_rejected_before_iam(tmp_path):
    rendered, manifest, _, receipt = _fixture_documents(tmp_path)
    iam = FakeIam()
    long_expiry = datetime.now(timezone.utc) + timedelta(hours=12)
    with pytest.raises(RuntimeError):
        _run(rendered, manifest, receipt, iam, sts=FakeSts(long_expiry))
    assert all(call[0] not in {"create_role", "put_role_policy"} for call in iam.calls)


def test_expired_session_is_rejected_before_iam(tmp_path):
    rendered, manifest, _, receipt = _fixture_documents(tmp_path)
    iam = FakeIam()
    expired = datetime.now(timezone.utc) - timedelta(minutes=1)
    with pytest.raises(RuntimeError):
        _run(rendered, manifest, receipt, iam, sts=FakeSts(expired))
    assert all(call[0] not in {"create_role", "put_role_policy"} for call in iam.calls)


def test_wrong_account_or_principal_prevents_mfa(tmp_path):
    rendered, manifest, _, receipt = _fixture_documents(tmp_path)
    iam = FakeIam()
    wrong = FakeSts(datetime.now(timezone.utc) + timedelta(minutes=15), account="999999999999")
    with pytest.raises(RuntimeError):
        _run(rendered, manifest, receipt, iam, sts=wrong)
    assert wrong.calls == ["get_caller_identity"]


def test_existing_role_prevents_overwrite(tmp_path):
    rendered, manifest, _, receipt = _fixture_documents(tmp_path)
    iam = FakeIam(role_exists=True)
    with pytest.raises(RuntimeError):
        _run(rendered, manifest, receipt, iam)
    assert all(call[0] != "create_role" for call in iam.calls)


def test_policy_failure_attempts_delete_role(tmp_path):
    rendered, manifest, _, receipt = _fixture_documents(tmp_path)
    iam = FakeIam(put_error=True)
    with pytest.raises(RuntimeError):
        _run(rendered, manifest, receipt, iam)
    assert any(call[0] == "delete_role" for call in iam.calls)


def test_missing_code_or_serial_prevents_aws_calls(tmp_path):
    rendered, manifest, _, receipt = _fixture_documents(tmp_path)
    iam = FakeIam()
    with pytest.raises(RuntimeError):
        _run(rendered, manifest, receipt, iam, token_code="")
    with pytest.raises(RuntimeError):
        _run(rendered, manifest, receipt, iam, mfa_serial="")
    assert iam.calls == []


def test_temporary_credentials_are_cleared_on_success(tmp_path):
    rendered, manifest, _, receipt = _fixture_documents(tmp_path)
    sts = FakeSts(datetime.now(timezone.utc) + timedelta(minutes=15))
    iam = FakeIam()
    _run(rendered, manifest, receipt, iam, sts=sts)
    assert sts.last_credentials
    assert all(value == "" for value in sts.last_credentials.values())


def test_changed_policy_and_regenerated_manifest_still_fails_without_receipt_update(tmp_path):
    rendered, manifest, terraform, receipt = _fixture_documents(tmp_path)
    (rendered / bootstrap.TRUST_DOC).write_bytes(b'{"trust":"changed"}')
    changed = manifest_builder.build_manifest(rendered, terraform)
    manifest.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(Exception):
        bootstrap.prepare_reviewed_inputs(manifest, rendered, receipt)
