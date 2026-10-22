"""One websocket notification path for document-comment projections.

The comment store announces every row it creates or updates
(``announce_after_commit``), so no adapter sends the frame itself and a new
write path cannot leave a second open canvas stale. Two writes are outside it.
Anchor position saves (``comments.update_positions``) follow a text change on
every autosave and are not announced; a canvas picks up moved anchors when it
loads the document. Feedback batch state (send, retry) is announced when a due
batch closes. A person's comment is announced once, after it is delivered, so
a canvas that reloads on the frame never shows it as unsent.
"""

from __future__ import annotations

from loguru import logger

from infra import state
from infra.write_effects import after_commit
from infra.auth import is_owner_sentinel
from infra.queries import get_thread
from registry import models as registry_models


def _comment_change_audience(artifact_id: str, *, db=None) -> str | None:
    """Resolve one document event to all operators or its owning thread's user."""
    artifact = registry_models.get_artifact(artifact_id, db=db)
    if artifact is None:
        return None
    parent_thread_id = artifact["parent_thread_id"]
    if is_owner_sentinel(parent_thread_id):
        return "all"
    owner = get_thread(str(parent_thread_id), db=db)
    return str(owner["user_id"]) if owner and owner["user_id"] else None


def collect_due_user_delivery(deliveries: list[str], db, row, closed: bool) -> None:
    """Append a post-commit user delivery only when this row closed."""
    if closed and row["author_kind"] == "user":
        deliveries.append(str(row["artifact_id"]))


def publish_due_user_feedback(deliveries: list[str]) -> None:
    """Publish post-commit due-batch updates through the comment event."""
    for artifact_id in deliveries:
        broadcast_doc_comment_changed(artifact_id)


def announce_after_commit(
    connection, artifact_id: str, *, comment_id: str | None = None, author_thread_id: str | None = None,
) -> None:
    """Send the change frame once the comment write on ``connection`` commits.

    A rolled-back write announces nothing. The frame is looked up and sent on
    the thread that committed, which for every adapter is a database worker.
    A new agent comment names itself (``comment_id``, ``author_thread_id``) so a
    canvas can hold it for its presence lane before it reloads the list.
    """
    after_commit(
        connection,
        lambda: broadcast_doc_comment_changed(
            artifact_id, comment_id=comment_id, author_thread_id=author_thread_id,
        ),
    )


def _send(audience: str, message: dict) -> None:
    if audience == "all":
        state.broadcast_all(message)
    else:
        state.broadcast_to_user(audience, message)


def broadcast_doc_comment_changed(
    artifact_id: str, *, comment_id: str | None = None, author_thread_id: str | None = None, db=None,
) -> None:
    """Nudge every permitted open canvas after a comment projection changes."""
    try:
        audience = _comment_change_audience(artifact_id, db=db)
        if audience:
            message = {"type": "doc_comment_changed", "artifact_id": artifact_id}
            if comment_id and author_thread_id:
                message.update(comment_id=comment_id, author_thread_id=author_thread_id)
            _send(audience, message)
    except Exception as exc:
        logger.warning(f"doc-collab doc_comment_changed broadcast failed for {artifact_id}: {exc}")
