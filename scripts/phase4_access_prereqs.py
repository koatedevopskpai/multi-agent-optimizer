"""Read-only Phase 4 access prerequisites.

Checks the pinned account/region, selected subnet, stage profiles, proposed IAM
roles and whether the human IAM user has an assigned MFA device. It writes
identifiers only to the gitignored private file; public evidence contains
booleans/counts. No credentials or MFA codes are read or printed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROFILE = "default"
REGION = "eu-west-2"
ENV_FILE = Path.home() / ".config" / "cloud-projects" / "focused-delivery-recovery.env"
PRIVATE_DIR = REPO / "var" / "phase4"
PRIVATE_FILE = PRIVATE_DIR / "access-prerequisites.json"
PUBLIC_RUN = os.environ.get("PHASE4_EVIDENCE_RUN", "DR-P4L-20261103T173000Z")
PUBLIC_FILE = REPO / f"evidence/delivery-recovery/preparation/{PUBLIC_RUN}/operations/access-prerequisites.json"
ROLES = (
    "focused-delivery-recovery-bootstrap",
    "focused-delivery-recovery-deploy",
    "focused-delivery-recovery-operator",
    "focused-delivery-recovery-runtime",
)
PROFILES = ("focused-bootstrap", "focused-deploy", "focused-operator")


def _expected_account() -> str | None:
    if not ENV_FILE.is_file():
        return None
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("AWS_EXPECTED_ACCOUNT_ID="):
            return line.split("=", 1)[1].strip()
    return None


def _aws(service: str, args: list[str]) -> dict:
    command = [
        "aws",
        "--profile",
        PROFILE,
        "--region",
        REGION,
        "--no-cli-pager",
        service,
        *args,
        "--output",
        "json",
    ]
    env = dict(os.environ)
    env["AWS_PROFILE"] = PROFILE
    env["AWS_REGION"] = REGION
    env["AWS_DEFAULT_REGION"] = REGION
    completed = subprocess.run(command, capture_output=True, text=True, timeout=60, env=env)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip())
    return json.loads(completed.stdout or "{}")


def _identity() -> dict:
    return _aws("sts", ["get-caller-identity"])


def _profile_names() -> list[str]:
    completed = subprocess.run(
        ["aws", "configure", "list-profiles"],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "AWS_PROFILE": PROFILE, "AWS_REGION": REGION},
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip())
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _profile_setting(name: str) -> str:
    completed = subprocess.run(
        ["aws", "configure", "get", name, "--profile", PROFILE],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "AWS_PROFILE": PROFILE, "AWS_REGION": REGION},
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _role_exists(name: str) -> tuple[bool, str | None]:
    try:
        role = _aws("iam", ["get-role", "--role-name", name]).get("Role", {})
    except RuntimeError as exc:
        if "NoSuchEntity" in str(exc):
            return False, None
        raise
    return bool(role), role.get("Arn")


def _mfa_devices(arn: str) -> tuple[bool, list[str]]:
    match = re.search(r":user/(.+)$", arn)
    if not match:
        return False, []
    user_name = match.group(1)
    devices = _aws("iam", ["list-mfa-devices", "--user-name", user_name]).get("MFADevices", [])
    return True, [item.get("SerialNumber", "") for item in devices if item.get("SerialNumber")]


def _selected_subnet() -> dict:
    selection = json.loads((PRIVATE_DIR / "network-selection.json").read_text(encoding="utf-8"))
    subnet = _aws("ec2", ["describe-subnets", "--subnet-ids", selection["subnet_id"]])["Subnets"][0]
    route_table = _aws(
        "ec2", ["describe-route-tables", "--route-table-ids", selection["route_table_id"]]
    )["RouteTables"][0]
    nacl = _aws(
        "ec2", ["describe-network-acls", "--network-acl-ids", selection["nacl_id"]]
    )["NetworkAcls"][0]
    vpc = _aws("ec2", ["describe-vpcs", "--vpc-ids", selection["vpc_id"]])["Vpcs"][0]
    import phase4_network_selection as network  # local import; same read-only helpers

    amazon_dns = network._amazon_provided_dns(vpc)
    nacl_result = network._evaluate_nacl(nacl.get("Entries", []), amazon_dns)
    return {
        "selection": selection,
        "subnet_present": bool(subnet),
        "availability_zone": subnet.get("AvailabilityZone"),
        "az_matches_private_selection": subnet.get("AvailabilityZone")
        == selection.get("availability_zone"),
        "vpc_matches_private_selection": subnet.get("VpcId") == selection.get("vpc_id"),
        "map_public_ip_on_launch": bool(subnet.get("MapPublicIpOnLaunch")),
        "route_table_present": bool(route_table),
        "route_table_matches_private_selection": route_table.get("RouteTableId")
        == selection.get("route_table_id"),
        "has_igw_default_route": network._has_igw_route(route_table),
        "nacl_present": bool(nacl),
        "nacl_matches_private_selection": nacl.get("NetworkAclId") == selection.get("nacl_id"),
        "nacl_egress_443": nacl_result["egress_https"],
        "nacl_ingress_ephemeral": nacl_result["ingress_tcp_return"],
        "nacl_ingress_udp_ephemeral": nacl_result["ingress_udp_return"],
        "amazon_provided_dns_configured": amazon_dns,
        "dns_path_available": nacl_result["dns_path_available"],
        "nacl_supported_configuration": nacl_result["supported"],
    }


def main() -> int:
    expected = _expected_account()
    identity = _identity()
    account = identity.get("Account")
    if not expected or account != expected:
        raise SystemExit("expected account is not configured or does not match; refusing checks")

    arn = str(identity.get("Arn", ""))
    principal_kind = "iam-user" if ":user/" in arn else ("assumed-role" if ":assumed-role/" in arn else "other")
    profiles = _profile_names()
    profile_presence = {name: name in profiles for name in PROFILES}
    role_presence: dict[str, dict] = {}
    for name in ROLES:
        exists, role_arn = _role_exists(name)
        role_presence[name] = {"exists": exists, "arn": role_arn}
    mfa_supported, mfa_serials = _mfa_devices(arn)
    subnet = _selected_subnet()

    PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
    private = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "account_id": account,
        "principal_arn": arn,
        "principal_kind": principal_kind,
        "profiles": profiles,
        "profile_presence": profile_presence,
        "mfa_check_supported": mfa_supported,
        "mfa_serial_numbers": mfa_serials,
        "profile_mfa_serial_configured": bool(_profile_setting("mfa_serial")),
        "roles": role_presence,
        "subnet": {
            key: value
            for key, value in subnet.items()
            if key != "selection"
        }
        | {
            "vpc_id": subnet["selection"].get("vpc_id"),
            "subnet_id": subnet["selection"].get("subnet_id"),
            "route_table_id": subnet["selection"].get("route_table_id"),
            "nacl_id": subnet["selection"].get("nacl_id"),
        },
    }
    PRIVATE_FILE.write_text(
        json.dumps(private, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )

    public = {
        "schema": "phase4-access-prerequisites-v1",
        "checked_at_utc": private["checked_at_utc"],
        "profile": PROFILE,
        "region": REGION,
        "read_only": True,
        "account_matches_expected": True,
        "principal_kind": principal_kind,
        "mfa_check_supported": mfa_supported,
        "mfa_device_count": len(mfa_serials),
        "mfa_device_available": bool(mfa_serials),
        "profile_mfa_serial_configured": private["profile_mfa_serial_configured"],
        "stage_profiles": profile_presence,
        "proposed_roles": {name: {"exists": value["exists"]} for name, value in role_presence.items()},
        "selected_subnet": {
            "identifier_redacted": True,
            "availability_zone": subnet["availability_zone"],
            "subnet_present": subnet["subnet_present"],
            "az_matches_private_selection": subnet["az_matches_private_selection"],
            "vpc_matches_private_selection": subnet["vpc_matches_private_selection"],
            "map_public_ip_on_launch": subnet["map_public_ip_on_launch"],
            "route_table_present": subnet["route_table_present"],
            "route_table_matches_private_selection": subnet["route_table_matches_private_selection"],
            "has_igw_default_route": subnet["has_igw_default_route"],
            "nacl_present": subnet["nacl_present"],
            "nacl_matches_private_selection": subnet["nacl_matches_private_selection"],
            "nacl_egress_443": subnet["nacl_egress_443"],
            "nacl_ingress_ephemeral": subnet["nacl_ingress_ephemeral"],
            "nacl_ingress_udp_ephemeral": subnet["nacl_ingress_udp_ephemeral"],
            "amazon_provided_dns_configured": subnet["amazon_provided_dns_configured"],
            "dns_path_available": subnet["dns_path_available"],
            "nacl_supported_configuration": subnet["nacl_supported_configuration"],
        },
        "private_file": "var/phase4/access-prerequisites.json",
        "notes": [
            "Read-only checks only; no role, profile, credential or MFA device was created.",
            "No MFA code was requested and no authentication is claimed.",
            "Identifiers and MFA serial numbers are omitted from public evidence.",
            "Default profile region is not used for mutation; each command/stage pins eu-west-2.",
        ],
    }
    PUBLIC_FILE.parent.mkdir(parents=True, exist_ok=True)
    PUBLIC_FILE.write_text(
        json.dumps(public, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    print(json.dumps(public, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
