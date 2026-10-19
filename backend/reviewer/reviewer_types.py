"""Shared result types for terminal LLM reviewer outcomes."""

from __future__ import annotations

from typing import Any, Literal, TypedDict


class ReviewerVerdict(TypedDict):
    """Terminal reviewer verdict delivered back to the PR owner."""

    verdict: Literal["clean", "code_fail"]
    required_action: Literal["none", "approach_reset"]
    summary: str
    block_count: int
    flag_count: int
    nit_count: int
    finding_count: int
    model_label: str
    reviewer_version: str | None
    reviewer_standards: list[str]


class ReviewerSkipped(TypedDict):
    """Explicit non-review success result for override-skipped PRs."""

    verdict: Literal["skipped"]
    summary: str
    model_label: str


ReviewerResult = ReviewerVerdict | ReviewerSkipped
ReviewComments = tuple[list[dict[str, Any]], list[dict[str, Any]]]


def reviewer_verdict(
    verdict: Literal["clean", "code_fail"],
    required_action: Literal["none", "approach_reset"],
    summary: str,
    block_count: int,
    flag_count: int,
    nit_count: int,
    finding_count: int,
    model_label: str,
    reviewer_version: str | None,
    reviewer_standards: list[str] | None,
) -> ReviewerVerdict:
    """Build the terminal result delivered by the reviewer gate."""
    return {
        "verdict": verdict,
        "required_action": required_action,
        "summary": summary,
        "block_count": block_count,
        "flag_count": flag_count,
        "nit_count": nit_count,
        "finding_count": finding_count,
        "model_label": model_label,
        "reviewer_version": reviewer_version,
        "reviewer_standards": reviewer_standards or [],
    }
