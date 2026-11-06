"""Report statement, branch-only and combined coverage separately.

coverage.py's ``percent_covered`` (the usual "Cover" column) combines statement
and branch opportunities, so it must not be described as branch coverage. This
script reads ``coverage json`` output and prints all three figures.

The planned NFR-14 gate targets new candidate/approval/retry policy modules at
>=90% branch-only coverage:

    python -m coverage json -o coverage.json
    python scripts/check_coverage.py coverage.json \
        --require src/delivery_recovery/candidates.py=90
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_REQUIREMENTS = ("src/delivery_recovery/candidates.py=90",)


def _percent(covered: int, total: int) -> float:
    return 100.0 if total == 0 else (covered / total) * 100.0


def _normalise(path: str) -> str:
    return path.replace("\\", "/")


def _parse_requirement(value: str) -> tuple[str, float]:
    path, _, minimum = value.rpartition("=")
    if not path or not minimum:
        raise argparse.ArgumentTypeError(
            f"expected MODULE=PERCENT, got {value!r}"
        )
    try:
        return _normalise(path), float(minimum)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"expected MODULE=PERCENT, got {value!r}"
        ) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("coverage_json", type=Path)
    parser.add_argument(
        "--require",
        action="append",
        type=_parse_requirement,
        default=None,
        help="MODULE=MIN_BRANCH_PERCENT; may be repeated",
    )
    args = parser.parse_args(argv)

    data = json.loads(args.coverage_json.read_text(encoding="utf-8"))
    requirements = (
        args.require
        if args.require is not None
        else [_parse_requirement(item) for item in DEFAULT_REQUIREMENTS]
    )

    print(f"{'Path':<52} {'Statement':>10} {'Branch':>10} {'Combined':>10}")
    print("-" * 86)
    summaries: dict[str, dict[str, int | float]] = {}
    for path, payload in sorted(data["files"].items()):
        summary = payload["summary"]
        normalised = _normalise(path)
        summaries[normalised] = summary
        print(
            f"{normalised:<52} "
            f"{_percent(summary['covered_lines'], summary['num_statements']):>9.2f}% "
            f"{_percent(summary['covered_branches'], summary['num_branches']):>9.2f}% "
            f"{summary['percent_covered']:>9.2f}%"
        )

    totals = data["totals"]
    print("-" * 86)
    print(
        f"{'TOTAL':<52} "
        f"{_percent(totals['covered_lines'], totals['num_statements']):>9.2f}% "
        f"{_percent(totals['covered_branches'], totals['num_branches']):>9.2f}% "
        f"{totals['percent_covered']:>9.2f}%"
    )

    failures: list[str] = []
    for module, minimum in requirements:
        summary = summaries.get(module)
        if summary is None:
            failures.append(f"{module}: not present in coverage JSON")
            continue
        branch = _percent(summary["covered_branches"], summary["num_branches"])
        status = "PASS" if branch >= minimum else "FAIL"
        print(f"{status}: {module} branch-only {branch:.2f}% (required >= {minimum:.2f}%)")
        if branch < minimum:
            failures.append(
                f"{module}: branch-only {branch:.2f}% below {minimum:.2f}%"
            )

    if failures:
        print("\nCoverage gate failures:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
