"""Capture compact, dated AWS Price List and FX provenance for the cost packet."""

from __future__ import annotations

import json
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOCS = REPO / "var" / "phase4" / "cost-reports"
PROFILE = "default"
REGION = "eu-west-2"
LOCATION = "EU (London)"


def _price(service_code: str, args: list[str]) -> dict:
    command = [
        "aws",
        "pricing",
        "get-products",
        "--service-code",
        service_code,
        "--region",
        "us-east-1",
        "--profile",
        PROFILE,
        "--output",
        "json",
        "--max-results",
        "1",
        "--filters",
        *args,
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip())
    payload = json.loads(completed.stdout)
    product = json.loads(payload["PriceList"][0])
    dimensions = next(
        iter(
            next(iter(product["terms"]["OnDemand"].values()))["priceDimensions"].values()
        )
    )
    return {
        "status": "retrieved",
        "sku": product["product"].get("sku"),
        "usage_type": product["product"]["attributes"].get("usagetype"),
        "description": dimensions["description"],
        "unit": dimensions["unit"],
        "price_usd": float(dimensions["pricePerUnit"]["USD"]),
        "effective_date": next(
            iter(product["terms"]["OnDemand"].values())
        )["effectiveDate"],
    }


def main() -> int:
    retrieved_at = datetime.now(timezone.utc).isoformat()
    rates = {
        "ec2_t4g_medium_hour_usd": _price(
            "AmazonEC2",
            [
                "Type=TERM_MATCH,Field=instanceType,Value=t4g.medium",
                f"Type=TERM_MATCH,Field=location,Value={LOCATION}",
                "Type=TERM_MATCH,Field=operatingSystem,Value=Linux",
                "Type=TERM_MATCH,Field=tenancy,Value=Shared",
                "Type=TERM_MATCH,Field=preInstalledSw,Value=NA",
                "Type=TERM_MATCH,Field=capacitystatus,Value=Used",
            ]
        ),
        "ebs_gp3_gb_month_usd": _price(
            "AmazonEC2",
            [
                "Type=TERM_MATCH,Field=productFamily,Value=Storage",
                "Type=TERM_MATCH,Field=volumeApiName,Value=gp3",
                f"Type=TERM_MATCH,Field=location,Value={LOCATION}",
            ]
        ),
        "public_ipv4_hour_usd": _price(
            "AmazonVPC",
            [
                f"Type=TERM_MATCH,Field=location,Value={LOCATION}",
                "Type=TERM_MATCH,Field=usagetype,Value=EUW2-PublicIPv4:InUseAddress",
            ]
        ),
        "ebs_snapshot_gb_month_usd": _price(
            "AmazonEC2",
            [
                "Type=TERM_MATCH,Field=productFamily,Value=Storage Snapshot",
                "Type=TERM_MATCH,Field=usagetype,Value=EUW2-EBS:SnapshotUsage",
                f"Type=TERM_MATCH,Field=location,Value={LOCATION}",
            ]
        ),
        "ecr_storage_gb_month_usd": _price(
            "AmazonECR",
            [
                "Type=TERM_MATCH,Field=usagetype,Value=EUW2-TimedStorage-ByteHrs",
                f"Type=TERM_MATCH,Field=location,Value={LOCATION}",
            ]
        ),
        "cloudwatch_log_ingest_gb_usd": _price(
            "AmazonCloudWatch",
            [
                "Type=TERM_MATCH,Field=usagetype,Value=EUW2-DataProcessing-Bytes",
                f"Type=TERM_MATCH,Field=location,Value={LOCATION}",
            ]
        ),
        "cloudwatch_log_storage_gb_month_usd": _price(
            "AmazonCloudWatch",
            [
                "Type=TERM_MATCH,Field=usagetype,Value=EUW2-TimedStorage-ByteHrs",
                f"Type=TERM_MATCH,Field=location,Value={LOCATION}",
            ]
        ),
        "internet_egress_gb_usd": {
            "status": "assumption",
            "sku": None,
            "usage_type": None,
            "description": "1 GB internet egress allowance; verify the current AWS EC2 data-transfer page before deployment",
            "unit": "GB",
            "price_usd": 0.09,
            "effective_date": None,
        },
    }
    with urllib.request.urlopen(
        "https://api.frankfurter.app/latest?from=USD&to=GBP", timeout=20
    ) as response:
        fx = json.loads(response.read().decode("utf-8"))
    provenance = {
        "schema": "phase4-cost-provenance-v1",
        "retrieved_at_utc": retrieved_at,
        "region": REGION,
        "pricing_endpoint_region": "us-east-1",
        "profile": PROFILE,
        "rates": rates,
        "fx": {
            "base": "USD",
            "quote": "GBP",
            "rate": fx["rates"]["GBP"],
            "date": fx["date"],
            "source": "https://api.frankfurter.app/latest?from=USD&to=GBP",
        },
    }
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "phase4-pricing-provenance.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps({"retrieved_at_utc": retrieved_at, "fx": provenance["fx"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
