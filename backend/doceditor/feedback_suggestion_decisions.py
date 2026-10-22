"""Durable direct delivery for document suggestion decisions."""

from __future__ import annotations

from typing import Any

from doceditor.comment_resolve_rule import PERSON_COMMENT_RESOLVE_RULE
from doceditor.note_names import document_name
from infra.passive_changes import append_thread_context_note
from registry import models as registry_models

QUOTE_CHARS = 60


def _named(suggestion: dict[str, Any]) -> str:
    """A suggestion as its author can find it again: the text it replaced, clipped, and its id."""
    quote = " ".join(str(suggestion.get("anchor_text") or "").split())
    if len(quote) > QUOTE_CHARS:
        quote = quote[:QUOTE_CHARS - 1].rstrip() + "…"
    return f"“{quote}” ({suggestion['id']})" if quote else str(suggestion["id"])


def _decision_note(
    db, *, recipient_id: str, artifact_id: str, decided_by: str, suggestions: list[dict[str, Any]],
    decision_event_id: str, decision: str, reason: str | None,
) -> None:
    count = len(suggestions)
    document = document_name(registry_models.get_artifact(artifact_id, db=db))
    subject = "your suggestion" if count == 1 else f"{count} of your suggestions"
    summary = f"{decided_by} {decision} {subject} on {document}"
    # The person reads the transcript line, so it carries the decision alone and no "your" (whose would be unclear).
    shown = "a suggestion" if count == 1 else f"{count} suggestions"
    details: dict[str, Any] = {
        "decision_event_id": decision_event_id, "artifact_id": artifact_id,
        "chip": f"{decided_by} {decision} {shown} on {document}.",
    }
    if decision == "accepted":
        # Who resolves the person's comment a suggestion answers has one source; quote it, never restate it.
        summary += f": {', '.join(_named(suggestion) for suggestion in suggestions)}. {PERSON_COMMENT_RESOLVE_RULE}"
    else:
        summary += "."
        if reason:
            summary += f" Reason from the decider (verbatim): {reason}"
    append_thread_context_note(
        db,
        thread_id=recipient_id,
        summary=summary,
        details=details,
    )


def stranded_note(db, suggestion: dict[str, Any], *, editor: str) -> None:
    """Tell a suggestion's agent author that an edit it did not make left the suggestion nothing to apply to."""
    artifact_id = str(suggestion["artifact_id"])
    document = document_name(registry_models.get_artifact(artifact_id, db=db))
    append_thread_context_note(
        db,
        thread_id=str(suggestion["author_thread_id"]),
        summary=(
            f"{editor} changed the text your suggestion replaces on {document}, so it can no longer "
            f"be applied: {_named(suggestion)}. If it still applies, suggest again on the current text."
        ),
        details={
            "kind": "suggestion_stranded", "artifact_id": artifact_id, "comment_id": suggestion["id"],
            "chip": f"{editor}'s edit stranded a suggestion on {document}.",
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
