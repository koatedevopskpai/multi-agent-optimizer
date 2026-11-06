"""Tests for the standalone branch-only coverage checker."""

from __future__ import annotations

import json

from scripts.check_coverage import main


def _write_coverage(path, *, candidate_branch=100.0, contracts_branch=97.5):
    def summary(statement=100.0, branch=100.0, combined=100.0):
        total_branches = 10
        covered_branches = round(branch / 10)
        total_statements = 10
        covered_lines = round(statement / 10)
        return {
            "covered_lines": covered_lines,
            "num_statements": total_statements,
            "percent_statements_covered": statement,
            "covered_branches": covered_branches,
            "num_branches": total_branches,
            "percent_branches_covered": branch,
            "percent_covered": combined,
        }

    payload = {
        "files": {
            "src/delivery_recovery/candidates.py": {
                "summary": summary(branch=candidate_branch)
            },
            "src/delivery_recovery/contracts.py": {
                "summary": summary(branch=contracts_branch)
            },
        },
        "totals": summary(branch=90.0, combined=90.0),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_default_requirements_are_parsed_and_pass(tmp_path):
    coverage = _write_coverage(tmp_path / "coverage.json")

    assert main([str(coverage)]) == 0


def test_explicit_requirement_passes(tmp_path):
    coverage = _write_coverage(tmp_path / "coverage.json")

    assert main([str(coverage), "--require", "src/delivery_recovery/candidates.py=90"]) == 0


def test_default_requirement_fails_below_threshold(tmp_path):
    coverage = _write_coverage(tmp_path / "coverage.json", candidate_branch=50.0)

    assert main([str(coverage)]) == 1
