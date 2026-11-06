"""Semantic validation and deterministic fallback explanations."""

from __future__ import annotations

from ..contracts import RecoveryEvaluation
from .contracts import Explanation

__all__ = ["deterministic_explanation", "render_explanation", "validate_explanation"]


def validate_explanation(
    explanation: Explanation,
    evaluation: RecoveryEvaluation,
) -> list[str]:
    """Check the constrained explanation against authoritative domain facts."""

    errors: list[str] = []
    expected = evaluation.recommended_candidate_id

    if explanation.recommended_candidate_id != expected:
        errors.append(
            "recommended_candidate_id does not match the deterministic recommendation"
        )

    if explanation.recommended_candidate_id is None:
        return errors

    candidate = next(
        (
            item
            for item in evaluation.candidates
            if item.candidate_id == explanation.recommended_candidate_id
        ),
        None,
    )
    if candidate is None:
        errors.append("recommended_candidate_id is not a known candidate")
        return errors

    if explanation.finish_offset_workdays != candidate.finish_offset_workdays:
        errors.append("finish_offset_workdays does not match the verified candidate")
    if explanation.cost_pence != candidate.cost_pence:
        errors.append("cost_pence does not match the verified candidate")
    if not candidate.feasible:
        errors.append("a rejected candidate cannot be recommended")
    return errors


def render_explanation(
    evaluation: RecoveryEvaluation,
    candidate_id: str | None,
) -> str:
    """Render user-visible prose from verified facts only.

    Provider prose is never copied into the result. This is deliberately a
    narrow deterministic renderer rather than a regex-based claim checker.
    """

    if candidate_id is None:
        return (
            "No feasible catalogue option meets both the target finish and the "
            "recovery budget. The constraints are not relaxed."
        )

    candidate = next(
        (item for item in evaluation.candidates if item.candidate_id == candidate_id),
        None,
    )
    if candidate is None:
        return "No verified candidate is available for this recommendation."

    options = " + ".join(candidate.option_ids) if candidate.option_ids else "No change"
    return (
        f"{options} reaches finish day {candidate.finish_offset_workdays} at GBP "
        f"{candidate.cost_pence / 100:,.2f}. This was a pre-decision recommendation "
        "for human review; it records no manager decision."
    )


def deterministic_explanation(evaluation: RecoveryEvaluation) -> Explanation:
    """Produce a labelled deterministic explanation with no model claims."""

    candidate_id = evaluation.recommended_candidate_id
    if candidate_id is None:
        return Explanation(
            recommended_candidate_id=None,
            summary=render_explanation(evaluation, None),
        )

    candidate = next(
        item for item in evaluation.candidates if item.candidate_id == candidate_id
    )
    return Explanation(
        recommended_candidate_id=candidate.candidate_id,
        summary=render_explanation(evaluation, candidate.candidate_id),
        finish_offset_workdays=candidate.finish_offset_workdays,
        cost_pence=candidate.cost_pence,
    )
