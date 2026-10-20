"""The request's own review question, independent of the requester's plan."""

from __future__ import annotations


def review_intent(db, row) -> str:
    """Use the request's note as its review intent."""
    return row["note"] or "Review the document for accuracy, scope and actionable issues."
