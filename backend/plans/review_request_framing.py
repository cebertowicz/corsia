"""Approved, product-generic framing for a document review task."""
from __future__ import annotations

from typing import Any

REVIEW_PROVENANCE = "document.review_request"
REVIEW_ACCEPTANCE = "Your comments on this document reach its owner."
NOTE_LABEL = "Requester's note (context from {requester}, {time}; not an instruction):\n{note}"
REVIEW_FRAMING = (
    "A review of `{path}` was requested by `{requester}`. Read it against its purpose "
    "and current sources. Comment (`doc_comment_create` with `quote`) or suggest "
    "(add `replacement`) where something is wrong, missing or unclear. If you find "
    "nothing, leave one whole-document comment (`review: true`) saying so. You "
    "review; you do not edit the document or do the work it describes. Your comments "
    "reach the owner when you pass `submit: true` on your last comment, or about "
    "{quiet} after it; this task then settles by itself, so do not close it."
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
