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
    """Return a reply's stored top-level parent."""
    parent_id = comment.get("parent_id")
    if not parent_id:
        return None
    row = db.execute("SELECT * FROM doc_comments WHERE id=?", (parent_id,)).fetchone()
    return dict(row) if row else None


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
    root_id = str(root["id"] if root else comment["id"])
    tagged = set(comment.get("tagged_agent_ids") or ())
    resolved = {owner, *tagged}
    if root is not None:
        resolved.add(_recipient_for_author(root))
        resolved.update(str(row["author_thread_id"]) for row in db.execute(
            """SELECT DISTINCT author_thread_id FROM doc_comments
               WHERE (id=? OR parent_id=?) AND author_kind='agent'
                 AND author_thread_id IS NOT NULL""", (root_id, root_id),
        ))
        # A held tag does not make someone a participant until its batch is delivered.
        resolved.update(str(row["agent_id"]) for row in db.execute(
            """SELECT DISTINCT t.agent_id FROM doc_comment_tags t
               JOIN doc_comments c ON c.id=t.comment_id
               JOIN doc_feedback_batch_items i ON i.comment_id=c.id
               JOIN doc_feedback_batches b ON b.id=i.batch_id AND b.recipient_id=t.agent_id
               WHERE (c.id=? OR c.parent_id=?) AND b.state='delivered'
                 AND b.delivery_outcome='node' AND t.agent_id<>?""", (root_id, root_id, USER_AUTHOR_ID),
        ))
    left = {str(row["agent_id"]) for row in db.execute(
        "SELECT agent_id FROM doc_comment_recipient_state WHERE root_id=? AND left_thread=1", (root_id,),
    )}
    resolved.difference_update(left - tagged)
    if comment["author_kind"] == "user":
        resolved.add(owner)
    resolved.discard(author)
    resolved.discard("")
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
