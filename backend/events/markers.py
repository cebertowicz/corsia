"""Shared HTML comment markers for CORSIA's LLM reviewer output.

Single canonical source of truth — imported by both events.github_mapper
(comment noise-filter) and reviewer.reviewer_threads (comment authoring).
No dependencies on either layer.
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
