"""Static Phase 4 preparation invariants.

These checks are intentionally local and read-only. They verify that the
inspectable preparation assets preserve the reviewed security and cost
boundaries; they do not claim a deployment.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
DOCS = REPO / "docs" / "delivery-recovery"
TF = REPO / "infra" / "terraform"
IAM = REPO / "infra" / "iam"


def test_container_is_non_root_pinned_and_health_checked():
    text = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert "python:3.12-slim@sha256:" in text
    assert "node:24-bookworm-slim@sha256:" in text
    assert "phase4-py312.lock" in text
    assert "USER app" in text
    assert "HEALTHCHECK" in text
    assert "COPY --chown=app:app --from=web" in text
    assert "CMD [\"python\", \"scripts/run_phase3_demo.py\"" in text


def test_container_lock_is_linux_python_312_and_complete():
    text = (REPO / "requirements/phase4-py312.lock").read_text(encoding="utf-8")
    assert "python:3.12-slim" in text
    for package in (
        "fastapi==",
        "uvicorn==",
        "pydantic==",
        "langgraph==",
        "langgraph-checkpoint-sqlite==",
    ):
        assert package in text


def test_compose_binds_loopback_only_and_uses_persistent_data():
    data = yaml.safe_load((REPO / "compose.yaml").read_text(encoding="utf-8"))
    service = data["services"]["app"]
    assert service["ports"] == ["127.0.0.1:8000:8000"]
    assert service["container_name"] == "delivery-recovery"
    assert service["restart"] == "no"
    assert service["read_only"] is True
    assert "no-new-privileges:true" in service["security_opt"]
    assert "ALL" in service["cap_drop"]
    assert any("/data" in volume for volume in service["volumes"])
    assert "privileged" not in service


def test_terraform_preserves_single_instance_no_ingress_and_encryption():
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(TF.glob("*.tf"))
    )
    assert "aws_instance" in text
    assert "aws_ebs_volume" in text
    assert "aws_ecr_repository" in text
    assert "aws_cloudwatch_log_group" in text
    assert "encrypted" in text and "true" in text
    assert re.search(r'http_tokens\s*=\s*"required"', text)
    assert "aws_nat_gateway" not in text
    assert "aws_lb" not in text
    assert "ingress" not in text.lower()
    assert "associate_public_ip_address = true" in text
    assert "prevent_destroy = true" not in text
    assert 'cpu_credits = "standard"' in text
    assert "expected_account_id" in text
    assert "terraform_data" in text
    assert "bootstrap_archive_b64" in text
    assert "volume_id" in text
    assert "allowed_account_ids" in text
    assert "permissions_boundary" in text


def test_bootstrap_binds_volume_and_fails_closed_before_start():
    template = (TF / "templates/user-data.sh.tftpl").read_text(encoding="utf-8")
    assert "data-volume.sh prepare" in template
    assert "--expected-volume-id" in template
    assert "systemctl start delivery-recovery.service" in template
    assert "restart: \"no\"" in template
    assert "mount -a" not in template
    assert "nofail" not in template
    unit = (REPO / "infra/bootstrap/delivery-recovery.service").read_text(
        encoding="utf-8"
    )
    assert "ConditionPathIsMountPoint=/srv/delivery-recovery/data" in unit
    assert "ExecStartPre=" in unit
    assert "data-volume.sh verify" in unit


def test_backup_export_aborts_on_stop_or_mount_failure():
    text = (REPO / "infra/bootstrap/backup-export.sh").read_text(encoding="utf-8")
    assert 'stop || fail "service stop failed; backup aborted"' in text
    assert "service-control.sh verify" in text or '"$SERVICE_CONTROL" verify' in text
    assert "phase4_backup_restore.py create" in text
    assert "phase4_backup_restore.py export" in text


def test_restore_runbook_stages_inside_the_data_mount():
    text = (DOCS / "runbooks/backup-restore.md").read_text(encoding="utf-8")
    assert "/srv/delivery-recovery/data/restore/<run-id>" in text
    assert "restore-check" not in text
    assert "restore-switch.sh switch" in text


def test_iam_policies_parse_and_avoid_broad_administrative_actions():
    policies = list(IAM.glob("*.json"))
    assert policies
    for path in policies:
        document = json.loads(path.read_text(encoding="utf-8"))
        if path.name.endswith("trust-policy.json"):
            statements = document["Statement"]
            actions = json.dumps([statement.get("Action") for statement in statements])
            assert "sts:AssumeRole" in actions
            if "ec2.amazonaws.com" not in json.dumps(statements):
                assert "token.actions.githubusercontent.com" in json.dumps(statements) or any(
                    "aws:MultiFactorAuthPresent" in json.dumps(statement)
                    for statement in statements
                )
            continue
        statements = document["Statement"]
        assert statements
        actions = json.dumps([statement.get("Action") for statement in statements])
        for forbidden in ("iam:*", "ec2:*", "s3:*", "kms:*", "logs:*"):
            assert forbidden not in actions


def test_runbooks_and_review_packet_exist():
    for name in (
        "PHASE4_DEPLOYMENT_PLAN.md",
        "PHASE4_DEPLOYMENT_REVIEW_PACKET.md",
        "PHASE4_COST_ESTIMATE.md",
        "PHASE4_IAM_MATRIX.md",
        "PHASE4_ACCESS_AND_SEQUENCE.md",
        "runbooks/deploy.md",
        "runbooks/smoke.md",
        "runbooks/failure-recovery.md",
        "runbooks/rollback.md",
        "runbooks/backup-restore.md",
        "runbooks/pause-resume.md",
        "runbooks/teardown.md",
        "runbooks/recreation.md",
    ):
        path = DOCS / name
        assert path.is_file(), name
        assert path.read_text(encoding="utf-8").strip()


def test_cost_estimate_is_below_the_approved_ceiling_and_includes_vat():
    estimate = json.loads(
        (DOCS / "phase4-cost-estimate.json").read_text(encoding="utf-8")
    )
    assert estimate["includes_vat"] is True
    assert estimate["vat_rate"] == 0.2
    assert estimate["total_gbp"] < 20
    assert estimate["stop_threshold_gbp"] < 20
    assert estimate["total_gbp"] < estimate["stop_threshold_gbp"]
    assert estimate["currency"] == "GBP"
    assert estimate["credit_mode"] == "standard"
    assert estimate["scenario"]["interview_deployment_hours"] == 8.0
    assert estimate["scenario"]["compute_hours"] >= 32.0
    assert abs(
        estimate["compute_and_address_gbp"]
        + estimate["retained_recurring_gbp"]
        - estimate["subtotal_gbp"]
    ) < 0.01
    provenance = json.loads(
        (DOCS / "phase4-pricing-provenance.json").read_text(encoding="utf-8")
    )
    assert provenance["fx"]["date"]
    assert provenance["fx"]["rate"] > 0
    for name, rate in estimate["rate_provenance"].items():
        assert rate["status"] in {"retrieved", "assumption"}
        if rate["status"] == "retrieved":
            assert rate["sku"]
            assert rate["unit"]
            assert rate["price_usd"] > 0
    assert (
        estimate["rate_provenance"]["internet_egress_gb_usd"]["status"]
        == "assumption"
    )
