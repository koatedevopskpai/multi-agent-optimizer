"""IAM proposal renderer keeps private identifiers out of committed files."""

from __future__ import annotations

import json
from pathlib import Path

import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import phase4_render_iam as renderer  # noqa: E402


def test_render_replaces_all_private_placeholders(tmp_path):
    principals = {
        "bootstrap": "arn:aws:iam::123456789012:role/admin",
        "deployer": "arn:aws:iam::123456789012:user/deployer",
        "operator": "arn:aws:iam::123456789012:user/operator",
    }
    vpc_id = "vpc-0123456789abcdef0"
    for path in (REPO / "infra/iam").glob("*.json"):
        document = renderer.render(path, "123456789012", principals, vpc_id)
        text = json.dumps(document)
        for placeholder in renderer.PLACEHOLDERS:
            assert placeholder not in text
    output = renderer.render(
        REPO / "infra/iam/deploy-trust-policy.json",
        "123456789012",
        principals,
        vpc_id,
    )
    assert "arn:aws:iam::123456789012:user/deployer" in json.dumps(output)
    bootstrap_trust = renderer.render(
        REPO / "infra/iam/bootstrap-trust-policy.json",
        "123456789012",
        principals,
        vpc_id,
    )
    assert "arn:aws:iam::123456789012:role/admin" in json.dumps(bootstrap_trust)


def test_deploy_policy_binds_describe_vpc_attribute_to_selected_vpc():
    principals = {
        "bootstrap": "arn:aws:iam::123456789012:role/admin",
        "deployer": "arn:aws:iam::123456789012:user/deployer",
        "operator": "arn:aws:iam::123456789012:user/operator",
    }
    vpc_id = "vpc-0123456789abcdef0"
    document = renderer.render(
        REPO / "infra/iam/deploy-policy.json",
        "123456789012",
        principals,
        vpc_id,
    )
    statements = [
        item
        for item in document["Statement"]
        if item.get("Sid") == "DescribeSelectedVpcAttributes"
    ]
    assert len(statements) == 1
    statement = statements[0]
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == "ec2:DescribeVpcAttribute"
    assert statement["Resource"] == (
        f"arn:aws:ec2:eu-west-2:123456789012:vpc/{vpc_id}"
    )
    assert "<vpc-id>" not in json.dumps(document)


def test_render_rejects_missing_vpc_binding():
    principals = {
        "bootstrap": "arn:aws:iam::123456789012:role/admin",
        "deployer": "arn:aws:iam::123456789012:user/deployer",
        "operator": "arn:aws:iam::123456789012:user/operator",
    }
    try:
        renderer.render(
            REPO / "infra/iam/deploy-policy.json",
            "123456789012",
            principals,
        )
    except ValueError as error:
        assert "<vpc-id>" in str(error)
    else:
        raise AssertionError("missing VPC binding must be rejected")
