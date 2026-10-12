"""Typed results produced by the reviewer gate."""

from __future__ import annotations

from typing import Literal, TypedDict


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


class ReviewerSkipped(TypedDict):
    """Explicit non-review success result for override-skipped PRs."""

    verdict: Literal["skipped"]
    summary: str
    model_label: str


ReviewerResult = ReviewerVerdict | ReviewerSkipped


class ReviewerPresenceError(RuntimeError):
    """The verdict is published at the head, but its presence marker is not."""
