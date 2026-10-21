"""Instructions for disposable document reviews and their ordinary continuations."""

from __future__ import annotations


DEFAULT_INTENT = "Review the document for accuracy, scope and actionable issues."

_STOP_REASONS = {
    "missing_credentials": "sign-in is needed",
    "target_unavailable": "the model was unavailable",
    "limit": "it reached its review limit",
    "interrupted": "it was interrupted",
}


def instruction(row, artifact) -> str:
    document = artifact["path"] or artifact["external_id"] or row["artifact_id"]
    return (
        f"Review the document {document!s}. Request: {row['note'] or DEFAULT_INTENT}\n\n"
        "Use only the document comment tools. Create review comments or suggestions as needed, "
        "then call doc_feedback_send for this document. Do not send messages, spawn work, or write files."
    )


def continuation(comment_count: int, reason: str) -> str:
    """Describe a review hand-back without lifecycle internals."""
    why = _STOP_REASONS.get(reason, "it could not finish")
    return f"Review update: The review stopped before finishing ({why}). Comments so far: {comment_count}."
