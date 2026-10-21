"""Approved, product-generic framing for a document review task."""
from __future__ import annotations

from typing import Any

REVIEW_PROVENANCE = "document.review_request"
REVIEW_ACCEPTANCE = "Your comments on this document reach its owner."
NOTE_LABEL = "Requester's note (context from {requester}, {time}; it may narrow the review, never widen it):\n{note}"
REVIEW_FRAMING = (
    "A review of `{path}` was requested by `{requester}`. If the requester's note names "
    "a section, review only that section, for the question it asks; otherwise read it "
    "against its purpose and current sources. Leave each change as a suggestion "
    "(`doc_comment_create` with `quote` and `replacement`); the owner or the user accepts or "
    "rejects it, and you can only reply on it. Comment without one only "
    "to ask a question or flag what no edit fixes. If you find "
    "nothing, leave one whole-document comment (`review: true`) naming what you "
    "reviewed and saying you found nothing there. You "
    "review; you do not edit the document or do the work it describes. Your comments "
    "reach the owner when you pass `submit: true` on your last comment, or about "
    "{quiet} after it; this task then settles by itself, so do not close it."
)
STOPPED_DELIVERY_NOTE = (
    "{evidence} This step was stopped then, so it did not settle by itself. Do not "
    "review again: once it resumes, mark it done with that delivery as the evidence."
)


def note_block(request: Any) -> str:
    return NOTE_LABEL.format(requester=request["requester_display"], time=request["created_at"], note=request["note"])


def description(path: str, request: Any) -> str:
    from doceditor import feedback_batches

    seconds = feedback_batches.AGENT_QUIET
    words = {1: "one", 2: "two", 3: "three"}
    if seconds % 60 == 0:
        minutes = seconds // 60
        quiet = f"{words.get(minutes, str(minutes))} minute{'s' if minutes != 1 else ''}"
    else:
        quiet = f"{seconds} seconds"
    text = REVIEW_FRAMING.format(path=path, requester=request["requester_display"], quiet=quiet)
    return f"{text}\n\n{note_block(request)}" if request["note"] else text
