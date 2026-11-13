"""Comment-specific helpers used by the canonical feedback batch writer."""

from __future__ import annotations

from typing import Any

from doceditor import comment_acks
from doceditor.feedback_state import USER_AUTHOR_ID
from doceditor.note_names import document_name
from events.envelope import normalize
from registry import models as registry_models
from registry import exemption_restore
from registry.document_supervisor import comment_recipient, owner_kind
from infra import config


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


def _agent_authors(db, root_id: str) -> set[str]:
    return {str(row["author_thread_id"]) for row in db.execute(
        """SELECT DISTINCT author_thread_id FROM doc_comments
           WHERE (id=? OR parent_id=?) AND author_kind='agent'
             AND author_thread_id IS NOT NULL""", (root_id, root_id),
    )}


def _tagged_agents(db, root_id: str, *, delivered_only: bool) -> set[str]:
    if not delivered_only:
        return {str(row["agent_id"]) for row in db.execute(
            """SELECT DISTINCT t.agent_id FROM doc_comment_tags t JOIN doc_comments c ON c.id=t.comment_id
               WHERE c.id=? OR c.parent_id=?""", (root_id, root_id),
        )}
    return {str(row["agent_id"]) for row in db.execute(
        """SELECT DISTINCT t.agent_id FROM doc_comment_tags t
           JOIN doc_comments c ON c.id=t.comment_id
           JOIN doc_feedback_batch_items i ON i.comment_id=c.id
           JOIN doc_feedback_batches b ON b.id=i.batch_id AND b.recipient_id=t.agent_id
           WHERE (c.id=? OR c.parent_id=?) AND b.state='delivered'
             AND b.delivery_outcome='node'""", (root_id, root_id),
    )}


def _left_agents(db, root_id: str) -> set[str]:
    return {str(row["agent_id"]) for row in db.execute(
        "SELECT agent_id FROM doc_comment_recipient_state WHERE root_id=? AND left_thread=1", (root_id,),
    )}


def participants(db, root_id: str) -> set[str]:
    """Everyone who commented on or is tagged into a thread, who must acknowledge it before it resolves.

    The person counts by commenting or by an agent's tag of them; an agent by commenting or by any
    tag, delivered or not, so the thread cannot resolve before a tagged agent has had its say. An
    agent that left the thread is no longer in it, until a tag brings it back.
    """
    rows = db.execute(
        "SELECT author_kind,tagged_user_id FROM doc_comments WHERE id=? OR parent_id=?", (root_id, root_id),
    ).fetchall()
    people = {USER_AUTHOR_ID} if any(row["author_kind"] == "user" or row["tagged_user_id"] for row in rows) else set()
    agents = (_agent_authors(db, root_id) | _tagged_agents(db, root_id, delivered_only=False)) - _left_agents(db, root_id)
    return people | agents


def agreed(db, root_id: str) -> bool:
    """Everyone in the thread has acknowledged it."""
    people = participants(db, root_id)
    return bool(people) and people <= comment_acks.acknowledgers(db, root_id)


def recipients(
    db, comment: dict[str, Any], *, document_owner_only: bool = False,
) -> tuple[str, ...]:
    """Resolve feedback recipients, or only the document owner for a user reopen.

    An agent that left the thread or acknowledged it is not woken, unless this comment tags it;
    the document owner still gets the person's comments. The person's own acknowledgement never
    stops replies reaching them.
    """
    artifact = registry_models.get_artifact(comment["artifact_id"], db=db)
    # The supervisor the header shows receives them; on an agent-owned document the
    # owning agent, the one that can edit it, keeps receiving them too.
    owner = comment_recipient(db, artifact, workspace=config.WORKSPACE) if artifact else ""
    if document_owner_only:
        return (owner,)
    stored_owner = artifact.get("parent_thread_id") if artifact else None
    editor = str(stored_owner) if owner_kind(stored_owner) == "agent" else ""
    author = _recipient_for_author(comment)
    root = _root_comment(db, comment)
    root_id = str(root["id"] if root else comment["id"])
    tagged = set(comment.get("tagged_agent_ids") or ())
    resolved = {owner, editor, *tagged}
    if root is not None:
        resolved.add(_recipient_for_author(root))
        resolved.update(_agent_authors(db, root_id))
        # A held tag does not make someone a participant until its batch is delivered.
        resolved.update(_tagged_agents(db, root_id, delivered_only=True))
    quiet = _left_agents(db, root_id) | (comment_acks.acknowledgers(db, root_id) - {comment_acks.PERSON})
    resolved.difference_update(quiet - tagged)
    if comment["author_kind"] == "user":
        resolved.update((owner, editor))
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
