"""Pure creation rules shared by document-comment adapters."""
from __future__ import annotations

from typing import Any

from doceditor.comment_action_errors import CommentActionError
from doceditor.image_regions import check_region_shape


def validate_create(
    *, body: str | None, parent_id: str | None, review: bool, quote: str | None,
    replacement: str | None, doc_path: str | None, artifact_id: str | None, region: Any = None,
    insert_after: bool = False,
) -> str:
    """Validate the common create shape and return normalized body text."""
    text = (body or "").strip()
    check_region_shape(region=region, parent_id=parent_id, review=review, replacement=replacement)
    if insert_after and (replacement is None or region is not None):
        raise CommentActionError("suggestion_shape", "insert_after requires a text suggestion with quote and replacement.")
    if replacement is not None:
        if parent_id or review:
            raise CommentActionError(
                "suggestion_shape",
                "A suggestion replaces quoted text. Pass quote and replacement, without parent_id or review.",
            )
        if not (quote or "").strip():
            raise CommentActionError(
                "suggestion_quote",
                "A suggestion needs quote: the exact current text. To insert after it, set insert_after.",
            )
        if not insert_after and replacement == quote:
            raise CommentActionError(
                "suggestion_identical",
                "replacement is identical to quote, so there is nothing to suggest.",
            )
    elif not text:
        raise CommentActionError("empty_body", "Empty comment")
    if parent_id and review:
        raise CommentActionError("reply_review", "a reply cannot also be a whole-doc review")
    if (parent_id or review) and (quote or "").strip():
        raise CommentActionError("quote_shape", "quote only applies to a top-level anchored comment")
    if parent_id and (doc_path or artifact_id):
        raise CommentActionError("reply_target", "replies derive their document target from parent_id")
    if not parent_id and bool(doc_path) == bool(artifact_id):
        raise CommentActionError("target", "exactly one of doc_path or artifact_id is required")
    if not parent_id and not review and region is None and not (quote or "").strip():
        raise CommentActionError(
            "quote_required",
            "quote is required to anchor a top-level comment (on an image artifact, pass region instead)",
        )
    return text
