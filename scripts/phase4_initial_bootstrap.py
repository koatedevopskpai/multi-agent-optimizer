"""MFA-derived initial bootstrap executor (fixed actions only).

Uses boto3 for credential resolution, signing and API calls. The reviewed
manifest and bootstrap bytes are verified locally before any AWS call or MFA
prompt. The MFA code is entered privately with getpass and never persisted.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3

from phase4_iam_manifest_schema import load_manifest, verified_documents
from phase4_review_receipt import verify_accepted_inputs

REPO = Path(__file__).resolve().parents[1]
ENV_FILE = Path.home() / ".config" / "cloud-projects" / "focused-delivery-recovery.env"
PRIVATE_FILE = REPO / "var" / "phase4" / "access-prerequisites.json"
RENDERED_DIR = REPO / "var" / "phase4" / "iam"
MANIFEST_FILE = RENDERED_DIR / "active-manifest.json"
RECEIPT_FILE = REPO / "var" / "phase4" / "independent-review-receipt.json"
ROLE_NAME = "focused-delivery-recovery-bootstrap"
TRUST_DOC = "bootstrap-trust-policy.json"
POLICY_DOC = "bootstrap-policy.json"
DURATION_SECONDS = 900
MAX_CLOCK_TOLERANCE = 120


def prepare_reviewed_inputs(
    manifest_path: Path, rendered_dir: Path, receipt_path: Path
) -> dict[str, bytes]:
    """Verify the Codex-owned receipt, accepted manifest and exact bytes."""
    _, manifest = verify_accepted_inputs(
        receipt_path=receipt_path,
        manifest_path=manifest_path,
        rendered_dir=rendered_dir,
    )
    return verified_documents(manifest, rendered_dir, (TRUST_DOC, POLICY_DOC))


def _assert_identity(identity: dict, expected_account: str, expected_principal: str) -> None:
    if identity.get("Account") != expected_account:
        raise RuntimeError("source account does not match expected")
    if identity.get("Arn") != expected_principal:
        raise RuntimeError("source principal does not match expected")


def _session_credentials(token: dict) -> dict[str, str]:
    credentials = token.get("Credentials") or {}
    access = credentials.get("AccessKeyId")
    secret = credentials.get("SecretAccessKey")
    session = credentials.get("SessionToken")
    expiration = credentials.get("Expiration")
    if not access or not secret or not session or not expiration:
        raise RuntimeError("MFA session token response was incomplete")
    if not isinstance(expiration, datetime):
        raise RuntimeError("MFA session expiry was not a timestamp")
    return {
        "access_key": access,
        "secret_key": secret,
        "session_token": session,
        "expiration": expiration,
    }


def _validate_session_lifetime(expiration: datetime, now: datetime, requested: int) -> None:
    if expiration <= now + timedelta(seconds=60):
        raise RuntimeError("MFA session is expired or too close to expiry")
    maximum = now + timedelta(seconds=requested + MAX_CLOCK_TOLERANCE)
    if expiration > maximum:
        raise RuntimeError("MFA session lifetime exceeds the reviewed bound")


def run_initial_bootstrap(
    *,
    source_session,
    expected_account: str,
    expected_principal: str,
    mfa_serial: str,
    token_code: str,
    trust_bytes: bytes,
    policy_bytes: bytes,
    session_factory=boto3.Session,
    now: datetime | None = None,
    duration_seconds: int = DURATION_SECONDS,
) -> dict:
    if not token_code:
        raise RuntimeError("MFA token code is required")
    if not mfa_serial:
        raise RuntimeError("MFA serial is required")
    if not trust_bytes or not policy_bytes:
        raise RuntimeError("reviewed bootstrap documents are required")
    if duration_seconds != DURATION_SECONDS:
        raise RuntimeError("unexpected MFA session duration request")

    sts = source_session.client("sts", region_name="us-east-1")
    _assert_identity(sts.get_caller_identity(), expected_account, expected_principal)
    token = sts.get_session_token(
        SerialNumber=mfa_serial,
        TokenCode=token_code,
        DurationSeconds=duration_seconds,
    )
    token_credentials = token.get("Credentials") or {}
    session_credentials = _session_credentials(token)
    try:
        moment = now or datetime.now(timezone.utc)
        _validate_session_lifetime(session_credentials["expiration"], moment, duration_seconds)
        temporary_session = session_factory(
            aws_access_key_id=session_credentials["access_key"],
            aws_secret_access_key=session_credentials["secret_key"],
            aws_session_token=session_credentials["session_token"],
            region_name="us-east-1",
        )
        temporary_sts = temporary_session.client("sts", region_name="us-east-1")
        _assert_identity(temporary_sts.get_caller_identity(), expected_account, expected_principal)
        iam = temporary_session.client("iam", region_name="us-east-1")
        try:
            iam.get_role(RoleName=ROLE_NAME)
            raise RuntimeError("bootstrap role already exists; refusing to overwrite")
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code != "NoSuchEntity":
                raise
        iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=trust_bytes.decode("utf-8"),
        )
        try:
            iam.put_role_policy(
                RoleName=ROLE_NAME,
                PolicyName="focused-bootstrap",
                PolicyDocument=policy_bytes.decode("utf-8"),
            )
        except Exception:
            try:
                iam.delete_role(RoleName=ROLE_NAME)
            finally:
                raise
        return {
            "status": "bootstrap_role_created",
            "role": ROLE_NAME,
            "trust_document": TRUST_DOC,
            "policy_document": POLICY_DOC,
            "session_expires_utc": session_credentials["expiration"].isoformat(),
            "duration_seconds": duration_seconds,
        }
    finally:
        session_credentials.clear()
        for key in list(token_credentials):
            token_credentials[key] = ""


def _expected_account() -> str | None:
    if not ENV_FILE.is_file():
        return None
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("AWS_EXPECTED_ACCOUNT_ID="):
            return line.split("=", 1)[1].strip()
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--profile", default="default")
    parser.add_argument("--rendered-dir", type=Path, default=RENDERED_DIR)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_FILE)
    parser.add_argument("--receipt", type=Path, default=RECEIPT_FILE)
    parser.add_argument("--expected-account", default="")
    parser.add_argument("--expected-principal", default="")
    parser.add_argument("--mfa-serial", default="")
    args = parser.parse_args()
    if not args.execute:
        print("dry_run: pass --execute to create the bootstrap role after MFA entry")
        return 0

    # Local reviewed-input checks happen before session creation or MFA prompt.
    try:
        documents = prepare_reviewed_inputs(args.manifest, args.rendered_dir, args.receipt)
    except Exception as exc:
        print(f"review_input_failed: {exc}", file=sys.stderr)
        return 1

    private = json.loads(PRIVATE_FILE.read_text(encoding="utf-8"))
    expected_account = args.expected_account or _expected_account()
    expected_principal = args.expected_principal or private.get("principal_arn", "")
    mfa_serial = args.mfa_serial or (private.get("mfa_serial_numbers") or [""])[0]
    if not expected_account or not expected_principal or not mfa_serial:
        print("review_input_failed: account, principal or MFA serial missing", file=sys.stderr)
        return 1
    source_session = boto3.Session(profile_name=args.profile)
    token_code = getpass.getpass("MFA code (not echoed): ")
    try:
        result = run_initial_bootstrap(
            source_session=source_session,
            expected_account=expected_account,
            expected_principal=expected_principal,
            mfa_serial=mfa_serial,
            token_code=token_code,
            trust_bytes=documents[TRUST_DOC],
            policy_bytes=documents[POLICY_DOC],
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"bootstrap_failed: {exc}", file=sys.stderr)
        return 1
    finally:
        token_code = ""


if __name__ == "__main__":
    raise SystemExit(main())
