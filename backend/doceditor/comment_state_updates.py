"""The one writer for a comment's resolve, acknowledge and re-open, and the note its agent author gets."""

from __future__ import annotations

from typing import Any

from doceditor import comment_acks, comment_notifications, feedback_comments
from doceditor import comments as store
from doceditor.comment_feedback_transitions import reopen_queues_feedback
from doceditor.note_names import actor_name, document_name
from infra.passive_changes import append_thread_context_note
from registry import models as registry_models


def _transitions(before: dict[str, Any], patch: dict[str, Any], *, by_user: bool, acknowledged: bool) -> list[str]:
    verbs = []
    if patch.get("resolved") is True and not before.get("resolved"):
        verbs.append("resolved")
    if acknowledged and "resolved" not in verbs:
        verbs.append("acknowledged")
    if patch.get("resolved") is False and before.get("resolved"):
        if not (by_user and reopen_queues_feedback(before)):
            verbs.append("re-opened")
    return verbs


def _acknowledge(connection, before: dict[str, Any], acknowledged: bool, *,
                 actor_thread_id: str | None, user_name: str | None) -> bool:
    """Record or take back the actor's acknowledgement of the comment's thread; True when it changed."""
    root_id = str(before.get("parent_id") or before["id"])
    actor = actor_thread_id or comment_acks.PERSON
    changed = comment_acks.record(
        connection, root_id, actor, actor_name(connection, actor_thread_id, user_name), acknowledged)
    if changed:
        comment_notifications.announce_after_commit(connection, before["artifact_id"])
    return changed


def _everyone_acknowledged(connection, root: dict[str, Any]) -> bool:
    """Everyone in the thread has acknowledged it; an open suggestion is decided, never resolved this way."""
    if root.get("resolved") or (root.get("replacement") is not None and not root.get("decision")):
        return False
    return feedback_comments.agreed(connection, str(root["id"]))


def _completed_root(connection, before: dict[str, Any]) -> dict[str, Any] | None:
    """The thread's root when everyone in it has now acknowledged it, so it resolves."""
    root = before if not before.get("parent_id") else store.get_comment(str(before["parent_id"]), db=connection)
    return root if root is not None and _everyone_acknowledged(connection, root) else None


def resolve_after_leave(connection, root_id: str, agent_id: str) -> None:
    """An agent left the thread: resolve it if everyone still in it has acknowledged it.

    Agreement is settled only where its inputs change: an acknowledgement (``update_comment``), a
    participant leaving (here), and a new reply (``comments.create_with_status`` re-opens a thread
    that closed on agreement).
    """
    root = store.get_comment(root_id, db=connection)
    if root is not None and _everyone_acknowledged(connection, root):
        update_comment(connection, root_id, {"resolved": True}, actor_thread_id=agent_id)


def update_comment(
    connection, comment_id: str, patch: dict[str, Any], *,
    actor_thread_id: str | None = None, user_name: str | None = None,
) -> dict[str, Any] | None:
    """Write a comment patch and tell an agent author about a state change someone else made.

    ``acked`` is the actor's own acknowledgement of the whole thread (the person when
    ``actor_thread_id`` is None). The one that completes the thread resolves it here, through this
    same resolve, even while a reply waits in its writer line: that reply re-opens it when it posts
    (``comments.create_with_status``). An author it resolves for hears only that it was resolved.
    The before-state is read in the caller's transaction, so the verb always names a
    change that happened. A user's re-open that already queues feedback, and a decision (written by
    ``comment_actions``), send no note.
    """
    before = store.get_comment(comment_id, db=connection)
    if before is None:
        return None
    fields = {key: value for key, value in patch.items() if key != "acked"}
    row = store.update(comment_id, fields, db=connection) if fields else before
    if row is None:
        return None
    acknowledged = "acked" in patch and _acknowledge(
        connection, before, bool(patch["acked"]), actor_thread_id=actor_thread_id, user_name=user_name)
    if acknowledged:
        row = store.get_comment(comment_id, db=connection)
    if actor_thread_id and patch.get("resolved") is True and not before.get("resolved"):
        from doceditor.comment_recipients import reset_action
        reset_action(connection, before, actor_thread_id)
    agreed = acknowledged and bool(patch["acked"])
    root = _completed_root(connection, before) if agreed else None
    told_resolved = root is not None and root.get("author_thread_id") == before.get("author_thread_id")
    _note_author(connection, before, patch, actor_thread_id=actor_thread_id, user_name=user_name,
                 acknowledged=agreed and not told_resolved)
    if root is not None:
        resolved = update_comment(connection, str(root["id"]), {"resolved": True},
                                  actor_thread_id=actor_thread_id, user_name=user_name)
        if root["id"] == comment_id:
            row = resolved
    return row


def _note_author(connection, before: dict[str, Any], patch: dict[str, Any], *,
                 actor_thread_id: str | None, user_name: str | None, acknowledged: bool) -> None:
    author = before.get("author_thread_id")
    if before.get("author_kind") != "agent" or not author or author == actor_thread_id:
        return
    artifact = registry_models.get_artifact(before["artifact_id"], db=connection)
    name = document_name(artifact)
    verbs = _transitions(before, patch, by_user=actor_thread_id is None, acknowledged=acknowledged)
    for verb in verbs:
        append_thread_context_note(
            connection, thread_id=str(author),
            summary=f"{actor_name(connection, actor_thread_id, user_name)} {verb} your comment on {name}.",
            details={"comment_id": before["id"], "artifact_id": before["artifact_id"]},
            actor_thread_id=actor_thread_id,
        )
