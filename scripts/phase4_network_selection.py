"""Read-only eu-west-2 subnet/route/NACL selection for Phase 4.

Actual identifiers are written to a gitignored private file. Public evidence
contains only selection criteria, booleans and counts.

NACL evaluation is deliberately narrow and honest:
- only IPv4 is evaluated for this default-VPC selection;
- public egress/return rules must cover 0.0.0.0/0 (narrower CIDRs do not prove
  general internet reachability);
- direction, protocol and first-match rule order are respected;
- the required TCP/UDP return interval 1024-65535 is checked in full, not by
  sampling one port;
- AmazonProvidedDNS is reported separately because resolver traffic to
  169.254.169.253 is not an ordinary NACL rule path.
"""

from __future__ import annotations

import ipaddress
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
PRIVATE_DIR = REPO / "var" / "phase4"
PRIVATE_FILE = PRIVATE_DIR / "network-selection.json"
PUBLIC_RUN = os.environ.get("PHASE4_EVIDENCE_RUN", "DR-P4L-20261103T173000Z")
PUBLIC_FILE = REPO / f"evidence/delivery-recovery/preparation/{PUBLIC_RUN}/operations/network-selection.json"
PUBLIC_NETWORK = ipaddress.ip_network("0.0.0.0/0")
EPHEMERAL_START = 1024
EPHEMERAL_END = 65535
ASSUMED_ROLE = re.compile(r"^arn:aws[a-z-]*:sts::(\d{12}):assumed-role/([^/]+)/")


def _expected_account() -> str | None:
    if not ENV_FILE.is_file():
        return None
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("AWS_EXPECTED_ACCOUNT_ID="):
            return line.split("=", 1)[1].strip()
    return None


def _aws(args: list[str]) -> dict:
    command = [
        "aws",
        "--profile",
        PROFILE,
        "--region",
        REGION,
        "--no-cli-pager",
        "ec2",
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
    completed = subprocess.run(
        [
            "aws",
            "--profile",
            PROFILE,
            "--region",
            REGION,
            "--no-cli-pager",
            "sts",
            "get-caller-identity",
            "--output",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "AWS_PROFILE": PROFILE, "AWS_REGION": REGION},
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip())
    return json.loads(completed.stdout)


def _assert_expected_account(expected: str | None, actual: str | None) -> None:
    """Abort before selection or output when the account is not pinned and equal."""
    if not expected:
        raise SystemExit("expected account is not configured; refusing selection")
    if not actual or actual != expected:
        raise SystemExit("authenticated account does not match expected; refusing selection")


def _route_table_for(subnet_id: str, vpc_id: str) -> dict | None:
    tables = _aws(["describe-route-tables", "--filters", f"Name=vpc-id,Values={vpc_id}"])
    main = None
    for table in tables.get("RouteTables", []):
        associations = table.get("Associations", [])
        if any(item.get("SubnetId") == subnet_id for item in associations):
            return table
        if any(item.get("Main") is True for item in associations):
            main = table
    return main


def _has_igw_route(table: dict | None) -> bool:
    if table is None:
        return False
    return any(
        route.get("DestinationCidrBlock") == "0.0.0.0/0"
        and str(route.get("GatewayId", "")).startswith("igw-")
        and route.get("State") == "active"
        for route in table.get("Routes", [])
    )


def _rule_network(rule: dict, family: str):
    key = "CidrBlock" if family == "ipv4" else "Ipv6CidrBlock"
    value = rule.get(key)
    if not value:
        return None
    try:
        network = ipaddress.ip_network(value, strict=False)
    except ValueError:
        return None
    expected_version = 4 if family == "ipv4" else 6
    return network if network.version == expected_version else None


def _rule_protocol_matches(rule: dict, protocol: str) -> bool:
    rule_protocol = str(rule.get("Protocol", ""))
    return rule_protocol in {"-1", protocol}


def _rule_port_matches(rule: dict, port: int) -> bool:
    if str(rule.get("Protocol", "")) == "-1":
        return True
    port_range = rule.get("PortRange")
    if not port_range:
        return True
    return int(port_range.get("From", 0)) <= port <= int(port_range.get("To", 65535))


def _rule_matches_at(
    rule: dict,
    *,
    egress: bool,
    protocol: str,
    port: int,
    address: str,
    family: str,
) -> bool:
    if "Egress" not in rule or bool(rule.get("Egress")) != egress:
        return False
    network = _rule_network(rule, family)
    if network is None:
        return False
    try:
        if ipaddress.ip_address(address) not in network:
            return False
    except ValueError:
        return False
    return _rule_protocol_matches(rule, protocol) and _rule_port_matches(rule, port)


def _decision(
    rules: list[dict],
    *,
    egress: bool,
    protocol: str,
    port: int,
    address: str = "0.0.0.0",
    family: str = "ipv4",
) -> str:
    """Return allow/deny for one address/port using NACL first-match order."""
    for rule in sorted(rules, key=lambda item: int(item.get("RuleNumber", 32767))):
        if _rule_matches_at(
            rule,
            egress=egress,
            protocol=protocol,
            port=port,
            address=address,
            family=family,
        ):
            return "allow" if rule.get("RuleAction") == "allow" else "deny"
    return "deny"


def _space_port_permitted(
    rules: list[dict],
    *,
    egress: bool,
    protocol: str,
    start_port: int,
    end_port: int,
    remote_network: ipaddress.IPv4Network = PUBLIC_NETWORK,
    family: str = "ipv4",
) -> bool:
    """Prove every address/port in the requested space is allowed.

    First-match decisions can change only at CIDR or port-range boundaries.
    Evaluate one representative address/port in every resulting rectangle, so a
    narrower deny before a broad allow cannot be skipped.
    """
    if start_port > end_port or family != "ipv4":
        return False
    request_start = int(remote_network.network_address)
    request_end = int(remote_network.broadcast_address)
    network_points = {request_start, request_end + 1}
    port_points = {start_port, end_port + 1}
    for rule in rules:
        if "Egress" not in rule or bool(rule.get("Egress")) != egress:
            continue
        if not _rule_protocol_matches(rule, protocol):
            continue
        network = _rule_network(rule, family)
        if network is None:
            continue
        overlap_start = max(request_start, int(network.network_address))
        overlap_end = min(request_end, int(network.broadcast_address))
        if overlap_start > overlap_end:
            continue
        network_points.add(overlap_start)
        network_points.add(overlap_end + 1)
        if str(rule.get("Protocol", "")) == "-1" or not rule.get("PortRange"):
            continue
        low = max(start_port, int(rule["PortRange"].get("From", 0)))
        high = min(end_port, int(rule["PortRange"].get("To", 65535)))
        if low <= high:
            port_points.add(low)
            port_points.add(high + 1)
    for network_start in sorted(
        point for point in network_points if request_start <= point <= request_end
    ):
        address = str(ipaddress.ip_address(network_start))
        for port_start in sorted(
            point for point in port_points if start_port <= point <= end_port
        ):
            if _decision(
                rules,
                egress=egress,
                protocol=protocol,
                port=port_start,
                address=address,
                family=family,
            ) != "allow":
                return False
    return True


def _range_permitted(
    rules: list[dict],
    *,
    egress: bool,
    protocol: str,
    start: int,
    end: int,
    remote_network: ipaddress.IPv4Network = PUBLIC_NETWORK,
    family: str = "ipv4",
) -> bool:
    return _space_port_permitted(
        rules,
        egress=egress,
        protocol=protocol,
        start_port=start,
        end_port=end,
        remote_network=remote_network,
        family=family,
    )


def _amazon_dns_from_dhcp(options: dict) -> bool:
    for option in options.get("DhcpOptions", []):
        configurations = option.get("DhcpConfigurations") or option.get("DhcpConfiguration") or []
        for config in configurations:
            if config.get("Key") != "domain-name-servers":
                continue
            values = {item.get("Value") for item in config.get("Values", [])}
            if values & {"AmazonProvidedDNS", "169.254.169.253"}:
                return True
    return False


def _amazon_provided_dns(vpc: dict) -> bool:
    dhcp_id = vpc.get("DhcpOptionsId")
    if not dhcp_id:
        return False
    try:
        options = _aws(["describe-dhcp-options", "--dhcp-options-ids", dhcp_id])
    except RuntimeError:
        return False
    return _amazon_dns_from_dhcp(options)


def _evaluate_nacl(entries: list[dict], amazon_dns: bool) -> dict:
    egress_https = _space_port_permitted(
        entries, egress=True, protocol="6", start_port=443, end_port=443
    )
    egress_dns_udp = _space_port_permitted(
        entries, egress=True, protocol="17", start_port=53, end_port=53
    )
    egress_dns_tcp = _space_port_permitted(
        entries, egress=True, protocol="6", start_port=53, end_port=53
    )
    ingress_tcp_return = _range_permitted(
        entries,
        egress=False,
        protocol="6",
        start=EPHEMERAL_START,
        end=EPHEMERAL_END,
    )
    ingress_udp_return = _range_permitted(
        entries,
        egress=False,
        protocol="17",
        start=EPHEMERAL_START,
        end=EPHEMERAL_END,
    )
    ordinary_dns = (
        egress_dns_udp
        and egress_dns_tcp
        and ingress_udp_return
        and ingress_tcp_return
    )
    dns_path_available = ordinary_dns or amazon_dns
    return {
        "egress_https": egress_https,
        "egress_dns_udp": egress_dns_udp,
        "egress_dns_tcp": egress_dns_tcp,
        "ingress_tcp_return": ingress_tcp_return,
        "ingress_udp_return": ingress_udp_return,
        "ordinary_dns": ordinary_dns,
        "amazon_dns": amazon_dns,
        "dns_path_available": dns_path_available,
        "supported": egress_https and ingress_tcp_return and dns_path_available,
    }


def main() -> int:
    expected = _expected_account()
    identity = _identity()
    _assert_expected_account(expected, identity.get("Account"))

    vpcs = _aws(["describe-vpcs", "--filters", "Name=isDefault,Values=true"])
    if not vpcs.get("Vpcs"):
        raise SystemExit("default VPC not found")
    vpc = vpcs["Vpcs"][0]
    vpc_id = vpc["VpcId"]
    amazon_dns = _amazon_provided_dns(vpc)

    gateways = _aws(
        [
            "describe-internet-gateways",
            "--filters",
            f"Name=attachment.vpc-id,Values={vpc_id}",
        ]
    )
    nat_gateways = _aws(
        ["describe-nat-gateways", "--filter", f"Name=vpc-id,Values={vpc_id}"]
    )
    subnets = _aws(
        [
            "describe-subnets",
            "--filters",
            f"Name=vpc-id,Values={vpc_id}",
        ]
    )["Subnets"]
    nacls = _aws(["describe-network-acls", "--filters", f"Name=vpc-id,Values={vpc_id}"])[
        "NetworkAcls"
    ]
    nacl_by_id = {item["NetworkAclId"]: item for item in nacls}
    nacl_for_subnet = {}
    for item in nacls:
        for association in item.get("Associations", []):
            if association.get("SubnetId"):
                nacl_for_subnet[association["SubnetId"]] = item

    candidates = []
    for subnet in sorted(subnets, key=lambda item: (item["AvailabilityZone"], item["SubnetId"])):
        table = _route_table_for(subnet["SubnetId"], vpc_id)
        nacl = nacl_by_id.get(subnet.get("NetworkAclId")) or nacl_for_subnet.get(
            subnet["SubnetId"]
        )
        nacl_result = _evaluate_nacl(nacl.get("Entries", []) if nacl else [], amazon_dns)
        has_igw = _has_igw_route(table)
        public_ip = bool(subnet.get("MapPublicIpOnLaunch"))
        candidates.append(
            {
                "subnet_id": subnet["SubnetId"],
                "availability_zone": subnet["AvailabilityZone"],
                "map_public_ip_on_launch": public_ip,
                "route_table_id": table.get("RouteTableId") if table else None,
                "has_igw_default_route": has_igw,
                "nacl_id": nacl.get("NetworkAclId") if nacl else None,
                "nacl_egress_443": nacl_result["egress_https"],
                "nacl_egress_dns": nacl_result["egress_dns_udp"] and nacl_result["egress_dns_tcp"],
                "nacl_ingress_ephemeral": nacl_result["ingress_tcp_return"],
                "nacl_ingress_udp_ephemeral": nacl_result["ingress_udp_return"],
                "nacl_egress_dns_udp": nacl_result["egress_dns_udp"],
                "nacl_egress_dns_tcp": nacl_result["egress_dns_tcp"],
                "amazon_provided_dns_configured": nacl_result["amazon_dns"],
                "dns_path_available": nacl_result["dns_path_available"],
                "nacl_supported_configuration": nacl_result["supported"],
                "eligible": all(
                    (
                        public_ip,
                        has_igw,
                        nacl_result["egress_https"],
                        nacl_result["ingress_tcp_return"],
                        nacl_result["dns_path_available"],
                    )
                ),
            }
        )

    eligible = [item for item in candidates if item["eligible"]]
    if not eligible:
        raise SystemExit(
            "no subnet satisfied the reviewed connectivity prerequisites "
            "(unsupported CIDR/direction/range NACL or missing IGW/public IP)"
        )
    selected = eligible[0]

    PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
    private = {
        "selected_at_utc": datetime.now(timezone.utc).isoformat(),
        "profile": PROFILE,
        "region": REGION,
        "account_matches_expected": True,
        "vpc_id": vpc_id,
        "subnet_id": selected["subnet_id"],
        "availability_zone": selected["availability_zone"],
        "route_table_id": selected["route_table_id"],
        "nacl_id": selected["nacl_id"],
        "internet_gateway_present": bool(gateways.get("InternetGateways")),
        "nat_gateway_count": len(nat_gateways.get("NatGateways", [])),
        "amazon_provided_dns_configured": selected["amazon_provided_dns_configured"],
    }
    PRIVATE_FILE.write_text(json.dumps(private, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")

    public = {
        "schema": "phase4-network-selection-v2",
        "checked_at_utc": private["selected_at_utc"],
        "profile": PROFILE,
        "region": REGION,
        "read_only": True,
        "ip_family_evaluated": "ipv4",
        "account_matches_expected": True,
        "default_vpc_present": True,
        "internet_gateway_present": bool(gateways.get("InternetGateways")),
        "nat_gateway_count": len(nat_gateways.get("NatGateways", [])),
        "subnet_count": len(candidates),
        "eligible_subnet_count": len(eligible),
        "selected_subnet": {
            "identifier_redacted": True,
            "availability_zone": selected["availability_zone"],
            "map_public_ip_on_launch": selected["map_public_ip_on_launch"],
            "has_igw_default_route": selected["has_igw_default_route"],
            "nacl_egress_443": selected["nacl_egress_443"],
            "nacl_egress_dns": selected["nacl_egress_dns"],
            "nacl_ingress_ephemeral": selected["nacl_ingress_ephemeral"],
            "nacl_ingress_udp_ephemeral": selected["nacl_ingress_udp_ephemeral"],
            "nacl_egress_dns_udp": selected["nacl_egress_dns_udp"],
            "nacl_egress_dns_tcp": selected["nacl_egress_dns_tcp"],
            "amazon_provided_dns_configured": selected["amazon_provided_dns_configured"],
            "dns_path_available": selected["dns_path_available"],
            "nacl_supported_configuration": selected["nacl_supported_configuration"],
            "route_table_selected": True,
            "nacl_selected": True,
        },
        "private_selection_file": "var/phase4/network-selection.json",
        "notes": [
            "These are configured prerequisites, not reachability observed on an instance.",
            "IPv4 only; IPv6 CIDRs and IPv6-only subnets are not evaluated.",
            "Public egress/return requires 0.0.0.0/0 coverage; narrower-only CIDRs are not treated as general internet access.",
            "Overlapping narrower rules are evaluated by address/port partition, so a narrow deny before a broad allow is not accepted as full coverage.",
            "The full TCP/UDP return interval 1024-65535 is checked, not one sampled port.",
            "Ordinary NACL DNS fields are reported separately from the AmazonProvidedDNS exception at 169.254.169.253.",
            "No NAT gateway, load balancer or interface endpoint was created or selected.",
            "Actual VPC/subnet/route-table/NACL identifiers are omitted from public evidence.",
        ],
    }
    PUBLIC_FILE.parent.mkdir(parents=True, exist_ok=True)
    PUBLIC_FILE.write_text(json.dumps(public, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(public, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
