"""London-specific Phase 4 cost estimate from official AWS price dimensions.

This is an estimate, not a bill or an enforced cap. Values and retrieval dates
are recorded so the deployment packet can be reviewed independently.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOCS = REPO / "docs" / "delivery-recovery"

USD_TO_GBP = 0.74519
VAT_RATE = 0.20
HOURS_PER_MONTH = 730.0
DAYS_PER_MONTH = 30.0

RATES = {
    "ec2_t4g_medium_hour_usd": {
        "value": 0.0376,
        "unit": "instance-hour",
        "source": "AWS Price List API AmazonEC2, t4g.medium, Linux, shared, EU (London), retrieved 2026-11-03",
    },
    "ebs_gp3_gb_month_usd": {
        "value": 0.0928,
        "unit": "GB-month",
        "source": "AWS Price List API AmazonEC2 EUW2-EBS:VolumeUsage.gp3, retrieved 2026-11-03",
    },
    "public_ipv4_hour_usd": {
        "value": 0.005,
        "unit": "in-use public IPv4-hour",
        "source": "AWS Price List API AmazonVPC EUW2-PublicIPv4:InUseAddress, retrieved 2026-11-03",
    },
    "ebs_snapshot_gb_month_usd": {
        "value": 0.053,
        "unit": "GB-month",
        "source": "AWS Price List API AmazonEC2 EUW2-EBS:SnapshotUsage, retrieved 2026-11-03",
    },
    "ecr_storage_gb_month_usd": {
        "value": 0.10,
        "unit": "GB-month",
        "source": "AWS Price List API AmazonECR EUW2-TimedStorage-ByteHrs, retrieved 2026-11-03",
    },
    "cloudwatch_log_ingest_gb_usd": {
        "value": 0.5985,
        "unit": "GB ingested",
        "source": "AWS Price List API AmazonCloudWatch EUW2-DataProcessing-Bytes, retrieved 2026-11-03",
    },
    "cloudwatch_log_storage_gb_month_usd": {
        "value": 0.0315,
        "unit": "GB-month",
        "source": "AWS Price List API AmazonCloudWatch EUW2-TimedStorage-ByteHrs, retrieved 2026-11-03",
    },
    "internet_egress_gb_usd": {
        "value": 0.09,
        "unit": "GB",
        "source": "AWS EC2 on-demand internet egress estimate for eu-west-2; verify current pricing page before deployment",
    },
}

# Conservative lifecycle: two bounded sessions, short residual retention, then
# teardown. The interview is not scheduled, so no idle cloud infrastructure is
# planned during the waiting period.
SCENARIO = {
    "compute_hours": 32.0,
    "interview_deployment_hours": 8.0,
    "root_gb": 10.0,
    "data_gb": 20.0,
    "ebs_days": 14.0,
    "public_ipv4_hours": 32.0,
    "snapshot_gb": 2.0,
    "snapshot_days": 7.0,
    "ecr_gb": 0.5,
    "ecr_days": 14.0,
    "log_ingest_gb": 0.1,
    "log_storage_gb": 0.1,
    "log_days": 14.0,
    "internet_egress_gb": 1.0,
    "contingency_multiplier": 3.0,
}


def main() -> int:
    s = SCENARIO
    ebs_gb = s["root_gb"] + s["data_gb"]
    line_items_usd = {
        "ec2_compute": s["compute_hours"] * RATES["ec2_t4g_medium_hour_usd"]["value"],
        "ebs_gp3": ebs_gb
        * RATES["ebs_gp3_gb_month_usd"]["value"]
        * (s["ebs_days"] / DAYS_PER_MONTH),
        "public_ipv4": s["public_ipv4_hours"]
        * RATES["public_ipv4_hour_usd"]["value"],
        "ebs_snapshot": s["snapshot_gb"]
        * RATES["ebs_snapshot_gb_month_usd"]["value"]
        * (s["snapshot_days"] / DAYS_PER_MONTH),
        "ecr_storage": s["ecr_gb"]
        * RATES["ecr_storage_gb_month_usd"]["value"]
        * (s["ecr_days"] / DAYS_PER_MONTH),
        "cloudwatch_ingest": s["log_ingest_gb"]
        * RATES["cloudwatch_log_ingest_gb_usd"]["value"],
        "cloudwatch_storage": s["log_storage_gb"]
        * RATES["cloudwatch_log_storage_gb_month_usd"]["value"]
        * (s["log_days"] / DAYS_PER_MONTH),
        "internet_egress": s["internet_egress_gb"]
        * RATES["internet_egress_gb_usd"]["value"],
    }
    subtotal_usd = sum(line_items_usd.values())
    compute_and_address_usd = (
        line_items_usd["ec2_compute"] + line_items_usd["public_ipv4"]
    )
    retained_recurring_usd = subtotal_usd - compute_and_address_usd
    subtotal_gbp = subtotal_usd * USD_TO_GBP
    vat_gbp = subtotal_gbp * VAT_RATE
    total_gbp = subtotal_gbp + vat_gbp
    contingency_gbp = total_gbp * s["contingency_multiplier"]
    hourly_while_running_gbp = (
        RATES["ec2_t4g_medium_hour_usd"]["value"]
        + RATES["public_ipv4_hour_usd"]["value"]
    ) * USD_TO_GBP * (1 + VAT_RATE)
    stopped_daily_gbp = (
        ebs_gb * RATES["ebs_gp3_gb_month_usd"]["value"] / DAYS_PER_MONTH
        + s["ecr_gb"] * RATES["ecr_storage_gb_month_usd"]["value"] / DAYS_PER_MONTH
        + s["log_storage_gb"]
        * RATES["cloudwatch_log_storage_gb_month_usd"]["value"]
        / DAYS_PER_MONTH
    ) * USD_TO_GBP * (1 + VAT_RATE)

    estimate = {
        "schema": "phase4-cost-estimate-v1",
        "created_at_utc": "2026-11-03T06:45:00Z",
        "region": "eu-west-2",
        "currency": "GBP",
        "usd_to_gbp": USD_TO_GBP,
        "fx_source": "Frankfurter API USD->GBP 2026-11-02",
        "includes_vat": True,
        "vat_rate": VAT_RATE,
        "approved_total_ceiling_gbp": 20.0,
        "stop_threshold_gbp": 12.0,
        "scenario": s,
        "credit_mode": "standard",
        "rates": RATES,
        "line_items_usd": {
            name: round(value, 6) for name, value in line_items_usd.items()
        },
        "compute_and_address_usd": round(compute_and_address_usd, 4),
        "compute_and_address_gbp": round(compute_and_address_usd * USD_TO_GBP, 4),
        "retained_recurring_usd": round(retained_recurring_usd, 4),
        "retained_recurring_gbp": round(retained_recurring_usd * USD_TO_GBP, 4),
        "subtotal_usd": round(subtotal_usd, 4),
        "subtotal_gbp": round(subtotal_gbp, 4),
        "vat_gbp": round(vat_gbp, 4),
        "total_gbp": round(total_gbp, 4),
        "contingency_multiplier": s["contingency_multiplier"],
        "contingency_total_gbp": round(contingency_gbp, 4),
        "remaining_below_ceiling_gbp": round(20.0 - total_gbp, 4),
        "hourly_while_running_gbp": round(hourly_while_running_gbp, 6),
        "stopped_daily_gbp": round(stopped_daily_gbp, 6),
        "assumptions": [
            "One t4g.medium instance in Standard CPU-credit mode; no Unlimited surplus charges are budgeted. Sustained CPU bursts can be throttled.",
            "32 running hours covers deployment, smoke, failure/recovery, rollback, backup/restore, one recreation rehearsal and an 8-hour interview deployment.",
            "No NAT gateway, load balancer, VPC interface endpoint, Elastic IP or long-lived cloud backup.",
            "Public IPv4 is released when the instance stops; no Elastic IP is allocated.",
            "ECR and the log group are deleted at teardown; images are rebuilt from source and digest references for recreation.",
            "14 residual days are a conservative allowance between deployment and teardown; the waiting period retains no project cloud infrastructure.",
            "VAT is 20% and the USD/GBP rate is the recorded 2026-11-02 reference; actual billing may vary.",
            "The GBP20 ceiling is a user-approved spending limit, not an AWS Budget enforced cap. The operational stop threshold is GBP12.",
        ],
    }
    provenance_path = DOCS / "phase4-pricing-provenance.json"
    if provenance_path.is_file():
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        estimate["rate_provenance"] = provenance.get("rates", {})
        estimate["fx_provenance"] = provenance.get("fx", {})
        estimate["provenance_file"] = "phase4-pricing-provenance.json"
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "phase4-cost-estimate.json").write_text(
        json.dumps(estimate, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    rows = [
        "| Line | USD | GBP (ex VAT) |",
        "|---|---:|---:|",
    ]
    for name, value in line_items_usd.items():
        rows.append(f"| {name} | ${value:.4f} | £{value * USD_TO_GBP:.4f} |")
    rows += [
        f"| **Compute + public IPv4** | **${compute_and_address_usd:.4f}** | **£{compute_and_address_usd * USD_TO_GBP:.4f}** |",
        f"| **Retained storage/logs/snapshot** | **${retained_recurring_usd:.4f}** | **£{retained_recurring_usd * USD_TO_GBP:.4f}** |",
        f"| **Subtotal** | **${subtotal_usd:.4f}** | **£{subtotal_gbp:.4f}** |",
        f"| VAT (20%) | | £{vat_gbp:.4f} |",
        f"| **Total incl. VAT** | | **£{total_gbp:.4f}** |",
        f"| 3x contingency | | £{contingency_gbp:.4f} |",
    ]
    markdown = f"""# Phase 4 London cost estimate

Generated from `scripts/phase4_cost_estimate.py` on 3 November 2026. Region:
`eu-west-2`. Currency: GBP, including 20% VAT. USD/GBP reference
`{USD_TO_GBP}` from Frankfurter on 2 November 2026.

This is an estimate, not a bill or an AWS-enforced cap. The user-approved total
ceiling is **£20**; the operational stop threshold is **£12** to leave room for
billing delay, tax and cleanup.

Dated official price dimensions and FX provenance are in
`phase4-pricing-provenance.json`. The internet-egress line is explicitly an
assumption, not a retrieved price.

## Scenario

- One `t4g.medium` instance in **Standard CPU-credit mode** for **{s['compute_hours']:.0f} hours** across
  deployment, drills, rollback, restore, one recreation rehearsal and an
  8-hour interview deployment. Standard mode has no Unlimited surplus charge,
  but sustained bursts may be throttled.
- Public IPv4 for the same **{s['public_ipv4_hours']:.0f} hours** while running.
- Encrypted gp3 storage: {s['root_gb']:.0f} GB root + {s['data_gb']:.0f} GB data,
  retained for up to {s['ebs_days']:.0f} days before teardown.
- One short snapshot drill ({s['snapshot_gb']:.0f} GB for {s['snapshot_days']:.0f} days).
- ECR image {s['ecr_gb']:.1f} GB for {s['ecr_days']:.0f} days, then deleted.
- CloudWatch Logs: {s['log_ingest_gb']:.1f} GB ingest, {s['log_storage_gb']:.1f} GB stored for {s['log_days']:.0f} days.
- 1 GB internet egress allowance; no NAT gateway or load balancer.

{chr(10).join(rows)}

Running cost: **£{hourly_while_running_gbp:.4f}/hour** (compute + public IPv4
incl. VAT). Stopped residual cost: **£{stopped_daily_gbp:.4f}/day** (EBS, ECR,
log storage; excl. snapshots).

The 3x contingency total is **£{contingency_gbp:.4f}**, still below the £20
ceiling. This estimate must be revised if instance size, session length,
retention, NAT, load balancing or interface endpoints change.
"""
    (DOCS / "PHASE4_COST_ESTIMATE.md").write_text(
        markdown, encoding="utf-8", newline="\n"
    )
    print(json.dumps(
        {
            "total_gbp": estimate["total_gbp"],
            "contingency_total_gbp": estimate["contingency_total_gbp"],
            "stop_threshold_gbp": estimate["stop_threshold_gbp"],
        },
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
