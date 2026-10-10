"""Comment-specific helpers used by the canonical feedback batch writer."""

from __future__ import annotations

from typing import Any

from doceditor.feedback_state import USER_AUTHOR_ID
from doceditor.note_names import document_name
from events.envelope import normalize
from registry import models as registry_models
from registry import exemption_restore


def author_id(comment: dict[str, Any]) -> str:
    """Return the stable batch-author principal for one comment."""
    return (
        str(comment["author_thread_id"])
        if comment["author_kind"] == "agent"
        else USER_AUTHOR_ID
    )


def _root_comment(db, comment: dict[str, Any]) -> dict[str, Any] | None:
    parent_id = comment.get("parent_id")
    if not parent_id:
        return None
    row = db.execute("SELECT * FROM doc_comments WHERE id=?", (parent_id,)).fetchone()
    if row is None:
        return None
    root = dict(row)
    if root.get("parent_id"):
        row = db.execute(
            "SELECT * FROM doc_comments WHERE id=?", (root["parent_id"],)
        ).fetchone()
        return dict(row) if row else root
    return root


def _recipient_for_author(comment: dict[str, Any]) -> str:
    return (
        str(comment["author_thread_id"])
        if comment["author_kind"] == "agent"
        else "ui"
    )


def recipients(
    db, comment: dict[str, Any], *, document_owner_only: bool = False,
) -> tuple[str, ...]:
    """Resolve feedback recipients, or only the document owner for a user reopen."""
    artifact = registry_models.get_artifact(comment["artifact_id"], db=db)
    owner = str(artifact.get("parent_thread_id") or "") if artifact else ""
    if document_owner_only:
        return (owner,)
    author = _recipient_for_author(comment)
    root = _root_comment(db, comment)
    if root is None:
        return () if owner == author else (owner,)
    resolved = {owner, _recipient_for_author(root)}
    resolved.discard(author)
    return tuple(sorted(resolved))


def make_user_document_durable(db, comment: dict[str, Any]) -> None:
    """Keep a document durable, and in its owner's pane, after the user comments on it."""
    if comment["author_kind"] != "user":
        return
    db.execute(
        "UPDATE artifact_identities SET retention='durable' WHERE id=?",
        (comment["artifact_id"],),
    )
    # The comment is already written, so the document is exempt from here on.
    exemption_restore.restore_exempt_owner_row(
        db, artifact_id=comment["artifact_id"], trigger="user_comment",
    )


def emit_comment_created(db, comment: dict[str, Any]) -> None:
    """Append the passive artifact event for one newly created comment."""
    artifact = registry_models.get_artifact(comment["artifact_id"], db=db)
    if artifact is None:
        return
    raw = {
        "document_id": artifact["external_id"],
        "comment_id": comment["id"],
        "author": comment["author"],
        "anchor": comment.get("anchor_text"),
        "version": artifact.get("updated_at") or comment["created_at"],
    }
    envelope = normalize("document", raw)
    registry_models.record_artifact_change(
        artifact_id=comment["artifact_id"],
        internal_event=envelope.type,
        external_id=envelope.event_id,
        summary=f"{comment['author']} commented on {document_name(artifact)}.",
        provenance={**envelope.provenance, "event_id": envelope.event_id},
        actor_thread_id=comment.get("author_thread_id"),
        db=db,
    )
