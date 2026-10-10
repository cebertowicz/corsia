"""Shared HTML comment markers for CORSIA's LLM reviewer output.

Single canonical source of truth — imported by both events.github_mapper
(comment noise-filter) and reviewer.reviewer_threads (comment authoring).
No dependencies on either layer.
"""

REVIEW_SUMMARY_MARKER = "<!-- corsia-review-summary -->"
REVIEW_FP_MARKER_PREFIX = "<!-- corsia-review-fp:"
REVIEW_RESOLVED_MARKER = "<!-- corsia-review-resolved -->"
