"""Shared HTML comment markers carried by automated review comments.

Single canonical source of truth for the comment noise-filter in
events.github_mapper and the ``has_reviewer_marker`` event fact.
"""

REVIEW_SUMMARY_MARKER = "<!-- corsia-review-summary -->"
REVIEW_FP_MARKER_PREFIX = "<!-- corsia-review-fp:"
REVIEW_RESOLVED_MARKER = "<!-- corsia-review-resolved -->"


def has_reviewer_marker(body: str) -> bool:
    """True when a body carries a reviewer marker as the reviewer publishes it: opening a line."""
    return any(
        f"\n{marker}" in body or body.startswith(marker)
        for marker in (REVIEW_SUMMARY_MARKER, REVIEW_RESOLVED_MARKER, REVIEW_FP_MARKER_PREFIX)
    )
