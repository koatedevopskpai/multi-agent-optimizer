"""Read-only AWS prerequisite checks for the Phase 4 deployment packet.

No resource is created, changed or deleted. Account IDs, ARNs and principal
names are deliberately reduced to booleans/counts so the output can be kept in
public evidence.
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
PROJECT = "focused-delivery-recovery"
ENV_FILE = Path.home() / ".config" / "cloud-projects" / "focused-delivery-recovery.env"
EVIDENCE_RUN = os.environ.get("PHASE4_EVIDENCE_RUN", "DR-P4L-20261103T173000Z")


def _expected_account() -> str | None:
    if not ENV_FILE.is_file():
        return None
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("AWS_EXPECTED_ACCOUNT_ID="):
            return line.split("=", 1)[1].strip()
    return None


def _run(args: list[str]) -> dict:
    command = [
        "aws",
        "--profile",
        PROFILE,
        "--region",
        REGION,
        "--no-cli-pager",
        *args,
        "--output",
        "json",
    ]
    env = dict(os.environ)
    env["AWS_PROFILE"] = PROFILE
    env["AWS_REGION"] = REGION
    env["AWS_DEFAULT_REGION"] = REGION
    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=60, env=env
    )
    if completed.returncode != 0:
        message = completed.stderr.strip().splitlines()
        code = "Unknown"
        if message:
            match = re.search(r"\(([A-Za-z0-9]+)\)", message[-1])
            if match:
                code = match.group(1)
        return {"ok": False, "error": code}
    try:
        return {"ok": True, "data": json.loads(completed.stdout or "{}")}
    except json.JSONDecodeError:
        return {"ok": False, "error": "InvalidJSON"}


def _count(result: dict, key: str) -> int | None:
    if not result.get("ok"):
        return None
    return len(result["data"].get(key, []))


def main() -> int:
    expected = _expected_account()
    identity = _run(["sts", "get-caller-identity"])
    account_match = None
    principal_kind = None
    if identity.get("ok"):
        account = str(identity["data"].get("Account", ""))
        account_match = bool(expected and account == expected)
        arn = str(identity["data"].get("Arn", ""))
        if ":user/" in arn:
            principal_kind = "iam-user"
        elif ":role/" in arn:
            principal_kind = "iam-role"
        else:
            principal_kind = "other"
    elif expected:
        account_match = False

    regions = _run(
        ["ec2", "describe-regions", "--region-names", REGION]
    )
    default_vpc = _run(
        ["ec2", "describe-vpcs", "--filters", "Name=isDefault,Values=true"]
    )
    default_vpc_id = None
    if default_vpc.get("ok") and default_vpc["data"].get("Vpcs"):
        default_vpc_id = default_vpc["data"]["Vpcs"][0]["VpcId"]
    subnets = _run(["ec2", "describe-subnets"])
    public_subnets = 0
    if subnets.get("ok"):
        route_tables = _run(["ec2", "describe-route-tables"])
        public_vpc_ids = set()
        if route_tables.get("ok"):
            for table in route_tables["data"].get("RouteTables", []):
                if any(
                    route.get("GatewayId", "").startswith("igw-")
                    for route in table.get("Routes", [])
                ):
                    public_vpc_ids.add(table.get("VpcId"))
        public_subnets = sum(
            1
            for subnet in subnets["data"].get("Subnets", [])
            if subnet.get("VpcId") in public_vpc_ids
            and subnet.get("MapPublicIpOnLaunch") is True
        )

    instance_types = _run(
        [
            "ec2",
            "describe-instance-types",
            "--instance-types",
            "t4g.medium",
        ]
    )
    project_instances = _run(
        [
            "ec2",
            "describe-instances",
            "--filters",
            f"Name=tag:Project,Values={PROJECT}",
            "Name=instance-state-name,Values=pending,running,stopping,stopped",
        ]
    )
    project_volumes = _run(
        [
            "ec2",
            "describe-volumes",
            "--filters",
            f"Name=tag:Project,Values={PROJECT}",
        ]
    )
    project_snapshots = _run(
        [
            "ec2",
            "describe-snapshots",
            "--owner-ids",
            "self",
            "--filters",
            f"Name=tag:Project,Values={PROJECT}",
        ]
    )
    addresses = _run(["ec2", "describe-addresses"])
    ecr_repo = _run(
        ["ecr", "describe-repositories", "--repository-names", PROJECT]
    )
    log_groups = _run(
        [
            "logs",
            "describe-log-groups",
            "--log-group-name-prefix",
            "/aws/delivery-recovery",
        ]
    )
    ssm_instances = _run(["ssm", "describe-instance-information"])
    oidc = _run(["iam", "list-open-id-connect-providers"])
    user = _run(["iam", "get-user"])
    attached = {"ok": False, "error": "NotAvailable"}
    inline = {"ok": False, "error": "NotAvailable"}
    groups = {"ok": False, "error": "NotAvailable"}
    group_attached_names: list[str] = []
    group_inline_count = 0
    simulation = {"ok": False, "error": "NotAvailable"}
    attached_names: list[str] = []
    if user.get("ok"):
        user_name = user["data"].get("User", {}).get("UserName")
        if user_name:
            attached = _run(
                ["iam", "list-attached-user-policies", "--user-name", user_name]
            )
            inline = _run(["iam", "list-user-policies", "--user-name", user_name])
            groups = _run(["iam", "list-groups-for-user", "--user-name", user_name])
        if identity.get("ok"):
            simulation = _run(
                [
                    "iam",
                    "simulate-principal-policy",
                    "--policy-source-arn",
                    identity["data"]["Arn"],
                    "--action-names",
                    "ec2:RunInstances",
                    "ec2:CreateVolume",
                    "iam:CreateRole",
                    "iam:PassRole",
                    "ecr:CreateRepository",
                    "logs:CreateLogGroup",
                ]
            )
    if attached.get("ok"):
        attached_names = [
            item.get("PolicyName", "")
            for item in attached["data"].get("AttachedPolicies", [])
        ]
    if groups.get("ok"):
        for group in groups["data"].get("Groups", []):
            group_name = group.get("GroupName")
            if not group_name:
                continue
            group_attached = _run(
                ["iam", "list-attached-group-policies", "--group-name", group_name]
            )
            if group_attached.get("ok"):
                group_attached_names.extend(
                    item.get("PolicyName", "")
                    for item in group_attached["data"].get("AttachedPolicies", [])
                )
            group_inline = _run(
                ["iam", "list-group-policies", "--group-name", group_name]
            )
            if group_inline.get("ok"):
                group_inline_count += len(
                    group_inline["data"].get("PolicyNames", [])
                )
    quota = _run(
        [
            "service-quotas",
            "get-service-quota",
            "--service-code",
            "ec2",
            "--quota-code",
            "L-1216C47A",
        ]
    )

    report = {
        "schema": "phase4-aws-readiness-v1",
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "profile": PROFILE,
        "region": REGION,
        "read_only": True,
        "identity": {
            "ok": identity.get("ok", False),
            "error": identity.get("error"),
            "expected_account_match": account_match,
            "principal_kind": principal_kind,
        },
        "region": {
            "available": bool(regions.get("ok") and regions["data"].get("Regions")),
            "error": regions.get("error"),
        },
        "default_vpc": {
            "present": default_vpc_id is not None,
            "error": default_vpc.get("error"),
        },
        "subnets": {
            "total": _count(subnets, "Subnets"),
            "public_with_auto_ip": public_subnets,
            "error": subnets.get("error"),
        },
        "instance_type": {
            "t4g_medium_available": bool(
                instance_types.get("ok")
                and instance_types["data"].get("InstanceTypes")
            ),
            "error": instance_types.get("error"),
        },
        "existing_project_resources": {
            "instances": _count(project_instances, "Reservations"),
            "volumes": _count(project_volumes, "Volumes"),
            "snapshots": _count(project_snapshots, "Snapshots"),
            "ecr_repository_exists": bool(
                ecr_repo.get("ok") and ecr_repo["data"].get("repositories")
            ),
            "log_groups": _count(log_groups, "logGroups"),
            "elastic_ips": _count(addresses, "Addresses"),
        },
        "ssm": {
            "managed_instances": _count(ssm_instances, "InstanceInformationList"),
            "error": ssm_instances.get("error"),
        },
        "iam": {
            "user_identity_present": bool(user.get("ok")),
            "attached_policy_probe": attached.get("error")
            or ("ok" if attached.get("ok") else "unknown"),
            "attached_policy_count": len(attached_names) if attached.get("ok") else None,
            "administrator_access_attached": (
                "AdministratorAccess" in attached_names
                or "AdministratorAccess" in group_attached_names
            ),
            "inline_policy_count": (
                len(inline["data"].get("PolicyNames", []))
                if inline.get("ok")
                else None
            ),
            "group_count": (
                len(groups["data"].get("Groups", [])) if groups.get("ok") else None
            ),
            "group_attached_policy_count": len(group_attached_names),
            "group_inline_policy_count": group_inline_count,
            "github_oidc_provider_count": _count(oidc, "OpenIDConnectProviderList"),
            "simulated_actions": (
                {
                    item.get("EvalActionName"): item.get("EvalDecision")
                    for item in simulation["data"].get("EvaluationResults", [])
                }
                if simulation.get("ok")
                else None
            ),
            "simulation_error": simulation.get("error"),
            "error": user.get("error"),
        },
        "quotas": {
            "on_demand_standard_vcpu": (
                quota["data"].get("Quota", {}).get("Value")
                if quota.get("ok")
                else None
            ),
            "error": quota.get("error"),
        },
        "notes": [
            "A successful identity call is not proof of deployment authority.",
            "Simulated actions describe the current principal, not the proposed deploy policy; the proposal is validated statically in tests/test_phase4_iam_proposal.py.",
            "Counts describe only project-tagged or project-prefixed resources.",
            "No resource was created, changed or deleted by this check.",
        ],
    }
    out = REPO / f"evidence/delivery-recovery/preparation/{EVIDENCE_RUN}/operations/aws-readiness.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
