"""Instructions for disposable document reviews and their ordinary continuations."""

from __future__ import annotations


DEFAULT_INTENT = "Review the document for accuracy, scope and actionable issues."


def instruction(row, artifact) -> str:
    document = artifact["path"] or artifact["external_id"] or row["artifact_id"]
    return (
        f"Review the document {document!s}. Request: {row['note'] or DEFAULT_INTENT}\n\n"
        "Use only the document comment tools. Create review comments or suggestions as needed, "
        "then call doc_feedback_send for this document. Do not send messages, spawn work, or write files."
    )


def continuation(comment_ids: list[str], state: str, reason: str) -> str:
    written = ", ".join(comment_ids) or "none"
    return (
        f"Continue the requested review; comments already written: {written}"
        f". Fork state: {state} ({reason})."
    )
