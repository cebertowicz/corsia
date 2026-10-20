"""Durable direct delivery for document suggestion decisions."""

from __future__ import annotations

from typing import Any

from doceditor.note_names import document_name
from infra.passive_changes import append_thread_context_note
from registry import models as registry_models

QUOTE_CHARS = 60


def _named(suggestion: dict[str, Any]) -> str:
    """A suggestion as its author can find it again: the text it replaced, clipped, and its id."""
    quote = " ".join(str(suggestion.get("anchor_text") or "").split())
    if len(quote) > QUOTE_CHARS:
        quote = quote[:QUOTE_CHARS - 1].rstrip() + "…"
    return f"“{quote}” ({suggestion['id']})"


def _decision_note(
    db, *, recipient_id: str, artifact_id: str, decided_by: str, suggestions: list[dict[str, Any]],
    decision_event_id: str, decision: str, reason: str | None,
) -> None:
    count = len(suggestions)
    subject = "your suggestion" if count == 1 else f"{count} of your suggestions"
    summary = f"{decided_by} {decision} {subject} on {document_name(registry_models.get_artifact(artifact_id, db=db))}"
    if decision == "accepted":
        # The person's comment a suggestion answers stays open; resolving it is the author's (PERSON_COMMENT_RESOLVE_RULE).
        summary += ": " + ", ".join(_named(suggestion) for suggestion in suggestions) + (
            ". If it answers a comment of theirs, resolve that comment, naming the suggestion in your reply."
            if count == 1 else
            ". Where one answers a comment of theirs, resolve that comment, naming the suggestion in your reply.")
    else:
        summary += "."
        if reason:
            summary += f" Reason from the decider (verbatim): {reason}"
    append_thread_context_note(
        db,
        thread_id=recipient_id,
        summary=summary,
        details={
            "decision_event_id": decision_event_id, "artifact_id": artifact_id,
            "suggestion_ids": [str(suggestion["id"]) for suggestion in suggestions],
        },
    )


def record_suggestion_decisions(
    db,
    comments: list[dict[str, Any]],
    *,
    decision: str,
    decided_by: str,
    decided_by_thread_id: str | None,
    decision_event_id: str,
    reason: str | None,
) -> None:
    """Tell a suggestion's agent author about one decision with a passive note."""
    if decision not in {"accepted", "rejected"}:
        raise ValueError("suggestion decision must be 'accepted' or 'rejected'")
    agent_suggestions = [
        comment for comment in comments
        if comment.get("replacement") is not None
        and comment.get("author_kind") == "agent"
        and comment.get("author_thread_id")
        and comment.get("author_thread_id") != decided_by_thread_id
    ]
    if not agent_suggestions:
        return
    recipient_id = str(agent_suggestions[0]["author_thread_id"])
    artifact_id = str(agent_suggestions[0]["artifact_id"])
    if db.execute("SELECT 1 FROM threads WHERE id=?", (recipient_id,)).fetchone() is None:
        return
    _decision_note(
        db, recipient_id=recipient_id, artifact_id=artifact_id, decided_by=decided_by,
        suggestions=agent_suggestions, decision_event_id=decision_event_id,
        decision=decision, reason=reason,
    )
