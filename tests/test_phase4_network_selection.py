"""Behavioral tests for the read-only Phase 4 network prerequisite checker."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import phase4_network_selection as network  # noqa: E402


def _rule(
    number: int,
    *,
    egress: bool,
    action: str = "allow",
    protocol: str = "-1",
    cidr: str | None = "0.0.0.0/0",
    ipv6: str | None = None,
    ports: tuple[int, int] | None = None,
) -> dict:
    rule = {
        "RuleNumber": number,
        "Egress": egress,
        "RuleAction": action,
        "Protocol": protocol,
    }
    if cidr is not None:
        rule["CidrBlock"] = cidr
    if ipv6 is not None:
        rule["Ipv6CidrBlock"] = ipv6
    if ports is not None:
        rule["PortRange"] = {"From": ports[0], "To": ports[1]}
    return rule


def test_inbound_allow_all_does_not_make_outbound_https_or_dns_allowed():
    entries = [
        _rule(100, egress=False, action="allow"),
        _rule(100, egress=True, action="deny"),
    ]
    assert network._decision(entries, egress=True, protocol="6", port=443) == "deny"
    assert network._decision(entries, egress=True, protocol="17", port=53) == "deny"
    result = network._evaluate_nacl(entries, amazon_dns=False)
    assert result["egress_https"] is False
    assert result["egress_dns_udp"] is False
    assert result["egress_dns_tcp"] is False
    assert result["ordinary_dns"] is False
    assert result["supported"] is False


def test_amazon_provided_dns_is_reported_separately_from_ordinary_dns():
    entries = [
        _rule(100, egress=False, action="allow"),
        _rule(100, egress=True, action="deny"),
    ]
    result = network._evaluate_nacl(entries, amazon_dns=True)
    assert result["egress_dns_udp"] is False
    assert result["ordinary_dns"] is False
    assert result["amazon_dns"] is True
    assert result["dns_path_available"] is True
    assert result["egress_https"] is False
    assert result["supported"] is False


def test_direction_is_respected_in_both_directions():
    outbound_only = [_rule(100, egress=True, ports=(443, 443), protocol="6")]
    assert network._decision(outbound_only, egress=True, protocol="6", port=443) == "allow"
    assert network._decision(outbound_only, egress=False, protocol="6", port=443) == "deny"
    inbound_only = [_rule(100, egress=False, ports=(443, 443), protocol="6")]
    assert network._decision(inbound_only, egress=False, protocol="6", port=443) == "allow"
    assert network._decision(inbound_only, egress=True, protocol="6", port=443) == "deny"


def test_missing_direction_does_not_match():
    entries = [
        {
            "RuleNumber": 100,
            "RuleAction": "allow",
            "Protocol": "-1",
            "CidrBlock": "0.0.0.0/0",
        }
    ]
    assert network._decision(entries, egress=True, protocol="6", port=443) == "deny"
    assert network._decision(entries, egress=False, protocol="6", port=443) == "deny"


def test_cidr_scope_and_ip_family_are_respected():
    narrower = [_rule(100, egress=True, cidr="10.0.0.0/8")]
    ipv6_only = [_rule(100, egress=True, cidr=None, ipv6="::/0")]
    any_v4 = [_rule(100, egress=True, cidr="0.0.0.0/0")]
    assert network._decision(narrower, egress=True, protocol="6", port=443) == "deny"
    assert network._decision(ipv6_only, egress=True, protocol="6", port=443) == "deny"
    assert network._decision(any_v4, egress=True, protocol="6", port=443) == "allow"


def test_protocol_is_respected():
    tcp = [_rule(100, egress=True, protocol="6", ports=(53, 53))]
    any_protocol = [_rule(100, egress=True, protocol="-1")]
    assert network._decision(tcp, egress=True, protocol="6", port=53) == "allow"
    assert network._decision(tcp, egress=True, protocol="17", port=53) == "deny"
    assert network._decision(any_protocol, egress=True, protocol="17", port=53) == "allow"


def test_first_match_order_is_authoritative():
    deny_first = [
        _rule(100, egress=True, action="deny"),
        _rule(200, egress=True, action="allow", protocol="6", ports=(443, 443)),
    ]
    allow_first = [
        _rule(100, egress=True, action="allow", protocol="6", ports=(443, 443)),
        _rule(200, egress=True, action="deny"),
    ]
    assert network._decision(deny_first, egress=True, protocol="6", port=443) == "deny"
    assert network._decision(allow_first, egress=True, protocol="6", port=443) == "allow"


def test_full_ephemeral_range_is_required_not_one_sampled_port():
    full = [_rule(100, egress=False, protocol="6", ports=(1024, 65535))]
    one_port = [_rule(100, egress=False, protocol="6", ports=(30000, 30000))]
    split = [
        _rule(100, egress=False, protocol="6", ports=(1024, 30000)),
        _rule(200, egress=False, protocol="6", ports=(30001, 65535)),
    ]
    allow_then_deny = [
        _rule(100, egress=False, protocol="6", ports=(1024, 65535)),
        _rule(200, egress=False, action="deny", protocol="6", ports=(30000, 30000)),
    ]
    deny_then_allow = [
        _rule(100, egress=False, action="deny", protocol="6", ports=(30000, 30000)),
        _rule(200, egress=False, protocol="6", ports=(1024, 65535)),
    ]
    kwargs = {"egress": False, "protocol": "6", "start": 1024, "end": 65535}
    assert network._range_permitted(full, **kwargs) is True
    assert network._range_permitted(one_port, **kwargs) is False
    assert network._range_permitted(split, **kwargs) is True
    assert network._range_permitted(allow_then_deny, **kwargs) is True
    assert network._range_permitted(deny_then_allow, **kwargs) is False


def test_default_allow_all_nacl_passes_supported_configuration():
    entries = [
        _rule(100, egress=True, action="allow"),
        _rule(100, egress=False, action="allow"),
    ]
    result = network._evaluate_nacl(entries, amazon_dns=True)
    assert result["egress_https"] is True
    assert result["ingress_tcp_return"] is True
    assert result["ingress_udp_return"] is True
    assert result["ordinary_dns"] is True
    assert result["supported"] is True


def test_narrow_deny_before_broad_allow_is_detected():
    entries = [
        _rule(100, egress=True, action="deny", cidr="0.0.0.0/1"),
        _rule(200, egress=True, action="allow", cidr="0.0.0.0/0"),
        _rule(100, egress=False, action="allow"),
    ]
    assert network._space_port_permitted(
        entries, egress=True, protocol="6", start_port=443, end_port=443
    ) is False
    result = network._evaluate_nacl(entries, amazon_dns=False)
    assert result["egress_https"] is False
    assert result["supported"] is False


def test_broad_allow_before_narrow_deny_is_allowed():
    entries = [
        _rule(100, egress=True, action="allow", cidr="0.0.0.0/0"),
        _rule(200, egress=True, action="deny", cidr="0.0.0.0/1"),
        _rule(100, egress=False, action="allow"),
    ]
    assert network._space_port_permitted(
        entries, egress=True, protocol="6", start_port=443, end_port=443
    ) is True
    assert network._evaluate_nacl(entries, amazon_dns=False)["egress_https"] is True


def test_overlapping_cidr_respects_direction_and_ip_family():
    inbound_deny = [
        _rule(100, egress=False, action="deny", cidr="0.0.0.0/1"),
        _rule(200, egress=False, action="allow", cidr="0.0.0.0/0"),
        _rule(100, egress=True, action="allow"),
    ]
    result = network._evaluate_nacl(inbound_deny, amazon_dns=False)
    assert result["egress_https"] is True
    assert result["ingress_tcp_return"] is False

    ipv6_deny = [
        _rule(100, egress=True, action="deny", cidr=None, ipv6="::/1"),
        _rule(200, egress=True, action="allow", cidr="0.0.0.0/0"),
        _rule(100, egress=False, action="allow"),
    ]
    assert network._space_port_permitted(
        ipv6_deny, egress=True, protocol="6", start_port=443, end_port=443
    ) is True


def test_overlapping_cidr_respects_port_specific_deny():
    entries = [
        _rule(100, egress=True, action="deny", cidr="0.0.0.0/1", protocol="6", ports=(443, 443)),
        _rule(200, egress=True, action="allow", cidr="0.0.0.0/0"),
        _rule(100, egress=False, action="allow"),
    ]
    result = network._evaluate_nacl(entries, amazon_dns=False)
    assert result["egress_https"] is False
    assert result["egress_dns_udp"] is True
    assert result["egress_dns_tcp"] is True
    assert result["supported"] is False


def test_narrow_deny_on_return_range_is_detected():
    entries = [
        _rule(100, egress=False, action="deny", cidr="0.0.0.0/1", protocol="6", ports=(1024, 65535)),
        _rule(200, egress=False, action="allow", cidr="0.0.0.0/0"),
    ]
    assert network._range_permitted(
        entries, egress=False, protocol="6", start=1024, end=65535
    ) is False


def test_expected_account_must_be_configured_and_match():
    with pytest.raises(SystemExit):
        network._assert_expected_account(None, "123456789012")
    with pytest.raises(SystemExit):
        network._assert_expected_account("123456789012", "999999999999")
    with pytest.raises(SystemExit):
        network._assert_expected_account("123456789012", None)
    network._assert_expected_account("123456789012", "123456789012")


def test_amazon_provided_dns_parsing():
    options = {
        "DhcpOptions": [
            {
                "DhcpConfigurations": [
                    {
                        "Key": "domain-name-servers",
                        "Values": [{"Value": "AmazonProvidedDNS"}],
                    }
                ]
            }
        ]
    }
    other = {
        "DhcpOptions": [
            {
                "DhcpConfigurations": [
                    {
                        "Key": "domain-name-servers",
                        "Values": [{"Value": "10.0.0.2"}],
                    }
                ]
            }
        ]
    }
    assert network._amazon_dns_from_dhcp(options) is True
    assert network._amazon_dns_from_dhcp(other) is False
