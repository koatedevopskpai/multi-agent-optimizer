"""Validate the proposed IAM policies with positive and negative cases."""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
IAM = REPO / "infra" / "iam"


def _document(name: str) -> dict:
    return json.loads((IAM / name).read_text(encoding="utf-8"))


def _statements(document: dict) -> list[dict]:
    return document["Statement"]


def _actions(statements: list[dict]) -> set[str]:
    actions: set[str] = set()
    for statement in statements:
        value = statement.get("Action")
        if isinstance(value, str):
            actions.add(value)
        else:
            actions.update(value)
    return actions


def _resources(statement: dict) -> list[str]:
    value = statement.get("Resource")
    items = [value] if isinstance(value, str) else list(value or [])
    return [item.replace("<account-id>", "123456789012") for item in items]


def _condition_value(condition: dict, key: str, context: dict):
    if key in {"aws:ResourceTag/Project", "ssm:ResourceTag/Project", "ec2:ResourceTag/Project"}:
        return context.get("resource_tags", {}).get("Project")
    if key == "aws:RequestTag/Project":
        return context.get("request_tags", {}).get("Project")
    if key == "ec2:CreateAction":
        return context.get("create_action")
    if key == "iam:PassedToService":
        return context.get("passed_to_service")
    if key == "iam:PolicyARN":
        return context.get("policy_arn")
    return context.get(key)


def _statement_matches(statement: dict, action: str, resource: str, context: dict) -> bool:
    actions = statement.get("Action")
    actions = {actions} if isinstance(actions, str) else set(actions or [])
    if action not in actions:
        return False
    if not any(fnmatch.fnmatch(resource, pattern) for pattern in _resources(statement)):
        return False
    for operator, pairs in statement.get("Condition", {}).items():
        if operator != "StringEquals":
            return False
        for key, expected in pairs.items():
            actual = _condition_value({"StringEquals": pairs}, key, context)
            allowed = [expected] if isinstance(expected, str) else list(expected)
            if actual not in allowed:
                return False
    return True


def _decision(document: dict, action: str, resource: str, context: dict) -> str:
    if any(
        statement.get("Effect") == "Allow"
        and _statement_matches(statement, action, resource, context)
        for statement in _statements(document)
    ):
        return "allowed"
    return "denied"


def test_deploy_policy_has_no_runtime_privilege_expansion():
    statements = _statements(_document("deploy-policy.json"))
    actions = _actions(statements)
    for forbidden in (
        "iam:CreateRole",
        "iam:CreateInstanceProfile",
        "iam:PutRolePolicy",
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:DeleteRolePolicy",
    ):
        assert forbidden not in actions
    assert "iam:PassRole" in actions


def test_bootstrap_policy_contains_the_privileged_iam_lifecycle():
    document = _document("bootstrap-policy.json")
    actions = _actions(_statements(document))
    required = {
        "iam:CreateRole",
        "iam:CreateInstanceProfile",
        "iam:PutRolePolicy",
        "iam:AttachRolePolicy",
        "iam:CreatePolicy",
        "iam:CreatePolicyVersion",
        "iam:PassRole",
    }
    assert required <= actions
    text = json.dumps(document)
    assert "AmazonSSMManagedInstanceCore" in text
    assert "AmazonEC2ContainerRegistryReadOnly" not in text


def test_create_tags_cannot_target_unowned_resources():
    deploy = _document("deploy-policy.json")
    instance = "arn:aws:ec2:eu-west-2:123456789012:instance/i-unowned"
    assert (
        _decision(deploy, "ec2:CreateTags", instance, {}) == "denied"
    )
    assert (
        _decision(
            deploy,
            "ec2:CreateTags",
            instance,
            {"create_action": "RunInstances"},
        )
        == "allowed"
    )
    assert (
        _decision(
            deploy,
            "ec2:CreateTags",
            instance,
            {"resource_tags": {"Project": "focused-delivery-recovery"}},
        )
        == "allowed"
    )
    assert (
        _decision(
            deploy,
            "ec2:CreateTags",
            instance,
            {"resource_tags": {"Project": "someone-else"}},
        )
        == "denied"
    )
    volume = "arn:aws:ec2:eu-west-2:123456789012:volume/vol-project"
    snapshot = "arn:aws:ec2:eu-west-2:123456789012:snapshot/snap-project"
    assert (
        _decision(
            deploy,
            "ec2:CreateSnapshot",
            volume,
            {
                "resource_tags": {"Project": "focused-delivery-recovery"},
                "request_tags": {"Project": "focused-delivery-recovery"},
            },
        )
        == "allowed"
    )
    assert (
        _decision(
            deploy,
            "ec2:CreateSnapshot",
            volume,
            {"resource_tags": {"Project": "focused-delivery-recovery"}},
        )
        == "denied"
    )
    assert (
        _decision(
            deploy,
            "ec2:DeleteSnapshot",
            snapshot,
            {"resource_tags": {"Project": "focused-delivery-recovery"}},
        )
        == "allowed"
    )
    assert (
        _decision(
            deploy,
            "ec2:DeleteSnapshot",
            snapshot,
            {"resource_tags": {"Project": "someone-else"}},
        )
        == "denied"
    )


def test_run_instances_and_pass_role_positive_and_negative():
    deploy = _document("deploy-policy.json")
    instance = "arn:aws:ec2:eu-west-2:123456789012:instance/i-new"
    assert (
        _decision(deploy, "ec2:RunInstances", instance, {}) == "denied"
    )
    assert (
        _decision(
            deploy,
            "ec2:RunInstances",
            instance,
            {"request_tags": {"Project": "focused-delivery-recovery"}},
        )
        == "allowed"
    )
    runtime = "arn:aws:iam::123456789012:role/focused-delivery-recovery-runtime"
    other = "arn:aws:iam::123456789012:role/some-other-role"
    context = {
        "passed_to_service": "ec2.amazonaws.com",
        "policy_arn": "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
    }
    assert _decision(deploy, "iam:PassRole", runtime, context) == "allowed"
    assert _decision(deploy, "iam:PassRole", other, context) == "denied"
    bootstrap = _document("bootstrap-policy.json")
    assert (
        _decision(bootstrap, "iam:AttachRolePolicy", runtime, context)
        == "allowed"
    )
    bad_context = {
        **context,
        "policy_arn": "arn:aws:iam::aws:policy/AdministratorAccess",
    }
    assert (
        _decision(bootstrap, "iam:AttachRolePolicy", runtime, bad_context)
        == "denied"
    )


def test_provider_and_runtime_boundary_enforce_account_and_ceiling():
    versions = (REPO / "infra/terraform/versions.tf").read_text(encoding="utf-8")
    assert "allowed_account_ids" in versions
    assert "expected_account_id" in versions
    main = (REPO / "infra/terraform/main.tf").read_text(encoding="utf-8")
    assert "permissions_boundary" in main
    assert "aws_iam_policy.runtime_boundary" in main
    assert "log-group:${var.log_group_name}:*" in main
    assert "${aws_cloudwatch_log_group.app.arn}:*" not in main
    boundary_actions = _actions(_statements(_document("runtime-boundary-policy.json")))
    assert "ssm:UpdateInstanceInformation" in boundary_actions
    assert "ecr:BatchGetImage" in boundary_actions
    assert not any(action.startswith("iam:") for action in boundary_actions)
    assert "ec2:RunInstances" not in boundary_actions
    assert "ecr:PutImage" not in boundary_actions
    boundary = _document("runtime-boundary-policy.json")
    for statement in _statements(boundary):
        if statement.get("Sid") == "EcrPullProjectRepository":
            assert statement["Resource"] == (
                "arn:aws:ecr:eu-west-2:${account_id}:repository/focused-delivery-recovery"
            )
            assert set(statement["Action"]) == {
                "ecr:BatchCheckLayerAvailability",
                "ecr:BatchGetImage",
                "ecr:GetDownloadUrlForLayer",
            }
    assert 'resource "aws_iam_role_policy" "ecr_pull"' in main
    assert 'aws_iam_role_policy_attachment" "ecr_pull"' not in main
    assert "AmazonEC2ContainerRegistryReadOnly" not in main


def test_operator_actions_use_correct_resource_types():
    statements = _statements(_document("operator-policy.json"))
    by_action: dict[str, list[dict]] = {}
    for statement in statements:
        for action in statement.get("Action", []) if isinstance(
            statement.get("Action"), list
        ) else [statement.get("Action")]:
            by_action.setdefault(action, []).append(statement)
    instance = "arn:aws:ec2:eu-west-2:123456789012:instance/i-project"
    assert (
        _decision(
            _document("operator-policy.json"),
            "ssm:StartSession",
            instance,
            {"resource_tags": {"Project": "focused-delivery-recovery"}},
        )
        == "allowed"
    )
    assert (
        _decision(
            _document("operator-policy.json"),
            "ssm:StartSession",
            instance,
            {"resource_tags": {"Project": "other"}},
        )
        == "denied"
    )
    terminate = by_action["ssm:TerminateSession"][0]
    assert all(":session/" in resource for resource in _resources(terminate))
    invocation = by_action["ssm:GetCommandInvocation"][0]
    assert _resources(invocation) == ["*"]
    cancel = by_action["ssm:CancelCommand"][0]
    assert _resources(cancel) == ["*"]
    connection = by_action["ssm:GetConnectionStatus"][0]
    assert any(":instance/" in resource for resource in _resources(connection))
    assert connection["Condition"]["StringEquals"]["ssm:ResourceTag/Project"] == (
        "focused-delivery-recovery"
    )
    start = by_action["ssm:StartSession"]
    assert any("instance/" in json.dumps(statement.get("Resource")) for statement in start)
    assert any("document/AWS-StartPortForwardingSession" in json.dumps(statement.get("Resource")) for statement in start)


def test_ssm_action_resource_support_map_matches_official_semantics():
    """Custom policy checks, not AWS authorization simulation.

    Based on the AWS Service Authorization Reference for Systems Manager:
    GetCommandInvocation/CancelCommand have no resource-level support and need
    Resource "*"; GetConnectionStatus authorizes against instance/managed
    instance; session lifecycle uses session/*.
    """
    statements = _statements(_document("operator-policy.json"))
    expected = {
        "ssm:GetCommandInvocation": {"*"},
        "ssm:CancelCommand": {"*"},
        "ssm:GetConnectionStatus": {"arn:aws:ec2:eu-west-2:123456789012:instance/*"},
        "ssm:TerminateSession": {"arn:aws:ssm:eu-west-2:123456789012:session/*"},
        "ssm:ResumeSession": {"arn:aws:ssm:eu-west-2:123456789012:session/*"},
        "ssm:DescribeSessions": {"*"},
        "ssm:ListCommands": {"*"},
        "ssm:DescribeInstanceInformation": {"*"},
    }
    actual: dict[str, set[str]] = {}
    for statement in statements:
        actions = statement.get("Action")
        actions = [actions] if isinstance(actions, str) else actions
        for action in actions or []:
            if action in expected:
                actual.setdefault(action, set()).update(_resources(statement))
    for action, resources in expected.items():
        assert actual.get(action) == resources, (action, actual.get(action))


def test_matrix_document_exists_and_names_all_identities():
    text = (REPO / "docs/delivery-recovery/PHASE4_IAM_MATRIX.md").read_text(
        encoding="utf-8"
    )
    for identity in (
        "Local named-profile deployer",
        "GitHub OIDC deploy role",
        "Operator",
        "EC2 runtime role",
        "One-time IAM bootstrap",
    ):
        assert identity in text
    assert "evidence that the proposed policies work" in text
    assert "**not**" in text


def test_bootstrap_boundary_operations_and_trust_paths():
    bootstrap = _actions(_statements(_document("bootstrap-policy.json")))
    assert "iam:PutRolePermissionsBoundary" in bootstrap
    assert "iam:DeleteRolePermissionsBoundary" in bootstrap
    assert "iam:CreatePolicy" in bootstrap
    for forbidden in ("iam:CreateUser", "iam:CreateAccessKey", "iam:AttachUserPolicy"):
        assert forbidden not in bootstrap
    document = _document("bootstrap-policy.json")
    for role in ("focused-delivery-recovery-deploy", "focused-delivery-recovery-operator"):
        arn = f"arn:aws:iam::123456789012:role/{role}"
        assert _decision(document, "iam:CreateRole", arn, {}) == "allowed"
        assert _decision(document, "iam:PutRolePolicy", arn, {}) == "allowed"
        assert _decision(document, "iam:DeleteRole", arn, {}) == "allowed"
    unrelated = "arn:aws:iam::123456789012:role/unrelated-role"
    assert _decision(document, "iam:CreateRole", unrelated, {}) == "denied"
    deploy_trust = _document("deploy-trust-policy.json")
    deploy_text = json.dumps(deploy_trust)
    assert "sts:AssumeRoleWithWebIdentity" in deploy_text
    assert "aws:MultiFactorAuthPresent" in deploy_text
    assert "token.actions.githubusercontent.com" in deploy_text
    named_deploy = _document("deploy-named-profile-trust-policy.json")
    named_text = json.dumps(named_deploy)
    assert "sts:AssumeRole" in named_text
    assert "aws:MultiFactorAuthPresent" in named_text
    assert "token.actions.githubusercontent.com" not in named_text
    for name in ("bootstrap-trust-policy.json", "operator-trust-policy.json"):
        trust = _document(name)
        assert "sts:AssumeRole" in json.dumps(trust)
        assert "aws:MultiFactorAuthPresent" in json.dumps(trust)
        assert "AWS_ACCESS_KEY_ID" not in json.dumps(trust)


def test_instance_provider_reads_are_scoped_without_extra_writes():
    document = _document("deploy-policy.json")
    action = "ec2:DescribeInstanceAttribute"
    instance = "arn:aws:ec2:eu-west-2:123456789012:instance/i-0123456789abcdef0"
    project = {"resource_tags": {"Project": "focused-delivery-recovery"}}
    assert _decision(document, action, instance, project) == "allowed"
    assert _decision(document, action, instance, {}) == "denied"
    assert _decision(document, action, instance, {"resource_tags": {"Project": "unrelated"}}) == "denied"
    assert _decision(document, action, instance.replace("eu-west-2", "eu-west-1"), project) == "denied"
    assert _decision(document, action, instance.replace("123456789012", "999999999999"), project) == "denied"
    credits = "ec2:DescribeInstanceCreditSpecifications"
    assert _decision(document, credits, "*", {"ec2:Region": "eu-west-2"}) == "allowed"
    assert _decision(document, credits, "*", {"ec2:Region": "eu-west-1"}) == "denied"
    assert _decision(document, credits, "*", {}) == "denied"
    selected = [s for s in document["Statement"] if s.get("Sid") in {"ReadProjectInstanceAttributes", "ReadLondonInstanceCredits"}]
    assert len(selected) == 2
    assert _actions(selected) == {action, credits}
    assert all(s["Effect"] == "Allow" for s in selected)


def test_active_manifest_excludes_optional_oidc_and_pins_scope():
    manifest = json.loads(
        (REPO / "docs/delivery-recovery/phase4-active-iam-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["active_document_count"] == 9
    assert manifest["excluded_optional_oidc_count"] == 2
    assert "github-oidc-trust-policy.json" not in manifest["active_documents"]
    assert "deploy-trust-policy.json" not in manifest["active_documents"]
    assert set(manifest["excluded_optional_oidc"]) == {
        "github-oidc-trust-policy.json",
        "deploy-trust-policy.json",
    }
    validation = manifest.get("validation")
    if validation is not None:
        assert validation["status"] == "passed"
        assert validation["findings"] == 0
    assert len(manifest["terraform_generated_source"]["sha256"]) == 64
    text = json.dumps(manifest)
    assert "arn:aws:" not in text
    assert not __import__("re").search(r"\b\d{12}\b", text)


def test_describe_vpc_attribute_is_isolated_and_vpc_scoped():
    document = _document("deploy-policy.json")
    statements = _statements(document)
    matches = [
        item
        for item in statements
        if item.get("Sid") == "DescribeSelectedVpcAttributes"
    ]
    assert len(matches) == 1
    statement = matches[0]
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == "ec2:DescribeVpcAttribute"
    assert statement["Resource"] == (
        "arn:aws:ec2:eu-west-2:<account-id>:vpc/<vpc-id>"
    )
    assert statement.get("Condition") is None
    discovery = next(
        item for item in statements if item.get("Sid") == "ReadOnlyDiscovery"
    )
    discovery_actions = (
        {discovery["Action"]}
        if isinstance(discovery["Action"], str)
        else set(discovery["Action"])
    )
    assert "ec2:DescribeVpcAttribute" not in discovery_actions
    actions = _actions(statements)
    assert "ec2:DescribeVpcAttribute" in actions
    assert not any("*" in action for action in actions if action.startswith("ec2:Describe"))
