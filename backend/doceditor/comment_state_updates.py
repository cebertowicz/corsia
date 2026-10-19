"""The one writer for a comment's resolve, acknowledge and re-open, and the note its agent author gets."""

from __future__ import annotations

from typing import Any

from doceditor import comments as store
from doceditor.comment_feedback_transitions import reopen_queues_feedback
from doceditor.note_names import actor_name, document_name
from infra.passive_changes import append_thread_context_note
from registry import models as registry_models

# Who resolves a person's comment, told to agents by the feedback-node framing and doc_comment_update.
PERSON_COMMENT_RESOLVE_RULE = (
    "Leave the user's comment open for them to resolve, above all one you replied to; resolve it "
    "yourself only when they asked you to, or when you made exactly the change it asked for "
    "and had nothing to say."
)


def _transitions(before: dict[str, Any], patch: dict[str, Any], *, by_user: bool) -> list[str]:
    verbs = []
    if patch.get("resolved") is True and not before.get("resolved"):
        verbs.append("resolved")
    if patch.get("acked") is True and not before.get("acked") and "resolved" not in verbs:
        verbs.append("acknowledged")
    if patch.get("resolved") is False and before.get("resolved"):
        if not (by_user and reopen_queues_feedback(before)):
            verbs.append("re-opened")
    return verbs


def update_comment(
    connection, comment_id: str, patch: dict[str, Any], *,
    actor_thread_id: str | None = None, user_name: str | None = None,
) -> dict[str, Any] | None:
    """Write a comment patch and tell an agent author about a state change someone else made.

    The before-state is read in the caller's transaction, so the verb always
    names a change that happened. A user's re-open that already queues
    feedback, and a decision (written by ``comment_actions``), send no note.
    """
    before = store.get_comment(comment_id, db=connection)
    row = store.update(comment_id, patch, db=connection)
    if before is None or row is None:
        return row
    author = before.get("author_thread_id")
    if before.get("author_kind") != "agent" or not author or author == actor_thread_id:
        return row
    artifact = registry_models.get_artifact(row["artifact_id"], db=connection)
    name = document_name(artifact)
    for verb in _transitions(before, patch, by_user=actor_thread_id is None):
        append_thread_context_note(
            connection, thread_id=str(author),
            summary=f"{actor_name(connection, actor_thread_id, user_name)} {verb} your comment on {name}.",
            details={"comment_id": comment_id, "artifact_id": row["artifact_id"]},
            actor_thread_id=actor_thread_id,
        )
    return row
