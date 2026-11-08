"""London-specific Phase 4 cost estimate from official AWS price dimensions.

This is an estimate, not a bill or an enforced cap. Values and retrieval dates
are recorded so the deployment packet can be reviewed independently.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOCS = REPO / "var" / "phase4" / "cost-reports"

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

# Illustrative lab lifecycle, not an owner spending approval or measured bill.
SCENARIO = {
    "compute_hours": 32.0,
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
        "hourly_while_running_gbp": round(hourly_while_running_gbp, 6),
        "stopped_daily_gbp": round(stopped_daily_gbp, 6),
        "assumptions": [
            "One t4g.medium instance in Standard CPU-credit mode; no Unlimited surplus charges are budgeted. Sustained CPU bursts can be throttled.",
            "Illustrative 32-hour compute allowance for a disposable engineering lab.",
            "No NAT gateway, load balancer, VPC interface endpoint, Elastic IP or long-lived cloud backup.",
            "Public IPv4 is released when the instance stops; no Elastic IP is allocated.",
            "ECR and the log group are deleted at teardown; images are rebuilt from source and digest references for recreation.",
            "14 residual days are a conservative allowance between deployment and teardown; the waiting period retains no project cloud infrastructure.",
            "VAT is 20% and the USD/GBP rate is the recorded 2026-11-02 reference; actual billing may vary.",
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

    markdown = (
        "# Illustrative cloud cost estimate\n\n"
        "Dated example inputs; refresh prices, exchange rate, tax and retention before use. "
        "This is neither a measured bill nor a spending authorization.\n\n"
        f"Estimated total incl. VAT: GBP{total_gbp:.4f}; "
        f"contingency example: GBP{contingency_gbp:.4f}.\n"
    )
    (DOCS / "cost-estimate.md").write_text(markdown, encoding="utf-8")
    print(json.dumps({"total_gbp": estimate["total_gbp"], "contingency_total_gbp": estimate["contingency_total_gbp"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
