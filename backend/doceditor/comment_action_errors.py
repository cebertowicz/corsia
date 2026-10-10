"""Stable document-comment action errors shared across adapters."""

from __future__ import annotations


class CommentActionError(ValueError):
    """A stable, adapter-mapped comment action failure."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def decision_only_error(*, decision: str | None, all_from_author: bool, reason: str | None) -> str | None:
    """Name a decision-only field sent without a decision, so no adapter drops it silently."""
    if decision is None and (all_from_author or reason is not None):
        return f"{'all_from_author' if all_from_author else 'reason'} requires decision"
    return None
