"""Render scoped IAM proposal documents with private identifiers.

No AWS call is made. Output stays under the gitignored var/phase4 directory.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLACEHOLDERS = (
    "<account-id>",
    "${account_id}",
    "<vpc-id>",
    "<bootstrap-principal-arn>",
    "<deployer-principal-arn>",
    "<operator-principal-arn>",
)


def render(
    path: Path,
    account_id: str,
    principals: dict[str, str],
    vpc_id: str | None = None,
) -> dict:
    text = path.read_text(encoding="utf-8")
    replacements = {
        "<account-id>": account_id,
        "${account_id}": account_id,
        "<bootstrap-principal-arn>": principals["bootstrap"],
        "<deployer-principal-arn>": principals["deployer"],
        "<operator-principal-arn>": principals["operator"],
    }
    if vpc_id is not None:
        replacements["<vpc-id>"] = vpc_id
    for placeholder, value in replacements.items():
        text = text.replace(placeholder, value)
    unresolved = [item for item in PLACEHOLDERS if item in text]
    if unresolved:
        raise ValueError(f"unresolved placeholders: {unresolved}")
    return json.loads(text)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--bootstrap-principal-arn", required=True)
    parser.add_argument("--deployer-principal-arn", required=True)
    parser.add_argument("--operator-principal-arn", required=True)
    parser.add_argument("--out-dir", default=str(REPO / "var" / "phase4" / "iam"))
    parser.add_argument(
        "--vpc-id",
        default="",
        help="Selected VPC ID; defaults from var/phase4/network-selection.json.",
    )
    args = parser.parse_args()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    principals = {
        "bootstrap": args.bootstrap_principal_arn,
        "deployer": args.deployer_principal_arn,
        "operator": args.operator_principal_arn,
    }
    vpc_id = args.vpc_id
    if not vpc_id:
        selection = json.loads(
            (REPO / "var" / "phase4" / "network-selection.json").read_text(
                encoding="utf-8"
            )
        )
        vpc_id = selection["vpc_id"]
    for path in sorted((REPO / "infra" / "iam").glob("*.json")):
        document = render(path, args.account_id, principals, vpc_id)
        (out / path.name).write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    print(f"rendered {len(list((REPO / 'infra/iam').glob('*.json')))} documents to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
