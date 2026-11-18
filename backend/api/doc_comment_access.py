"""Who may use a document's comments, and which document a pane's comment request belongs to.

The comment routes share one ownership gate. A promote retires the working file's identity and
moves its comment state to the successor (``doceditor.comment_transfer``; for promotes made earlier,
``migration_199_to_200``), so a pane still open on the retired id is followed to the live document:
a write lands there or is refused with ``NOT_COMMENTABLE``, and a read lists the live document's comments.
"""

from __future__ import annotations

from fastapi import HTTPException

from infra.auth import may_access_artifact
from registry import models as registry_models

# The refusal of a comment write whose document moved to a place the writer can't use; the browser keeps the draft.
NOT_COMMENTABLE = "Comments can’t be added to this document. Your comment wasn’t sent, and your text is still here."


def _assert_owns_artifact(artifact_id: str, user_id: str, db) -> None:
    """Raise 404 unless ``user_id`` may access ``artifact_id``'s comments.

    The single ownership gate the comment routes share: artifact →
    ``parent_thread_id`` → ``threads.user_id``. Allowed when the caller owns
    the owning thread or the owner is a workspace/project sentinel (shared
    substrate). A real OTHER user's thread, or an absent artifact, is a 404 —
    foreign ids stay non-enumerable.
    """
    art = registry_models.get_artifact(artifact_id, db=db)
    if art is None or not may_access_artifact(art["parent_thread_id"], user_id, db=db):
        raise HTTPException(404, "Artifact not found")


def _accessible_live_id(artifact_id: str, user_id: str, db) -> str | None:
    """The live identity of the document ``artifact_id`` names, or None when the caller can't use one.

    ``artifact_id`` itself unless it was promoted, else the active identity its successor chain ends at;
    None when the chain ends nowhere or the caller may not access where it ends.
    """
    live = registry_models.live_identity(artifact_id, db=db)
    if live is not None and live != artifact_id:
        try:
            _assert_owns_artifact(live, user_id, db)
        except HTTPException:
            return None
    return live


def _artifact_for_write(artifact_id: str, user_id: str, db) -> str:
    """``_assert_owns_artifact`` for a write, returning the artifact the write belongs to.

    A write keyed by a retired id follows the document to its live identity. Refuses with 409 and
    ``NOT_COMMENTABLE`` when the chain ends nowhere the caller may write, so the browser keeps the draft
    instead of the write landing on a row nobody reads.
    """
    _assert_owns_artifact(artifact_id, user_id, db)
    live = _accessible_live_id(artifact_id, user_id, db)
    if live is None:
        raise HTTPException(409, NOT_COMMENTABLE)
    return live


def _artifact_for_read(artifact_id: str, user_id: str, db) -> str:
    """The artifact whose comments a pane on ``artifact_id`` lists: the live identity, the same from any of its ids.

    When no live document the caller may read exists, the id named (as before the promote).
    """
    _assert_owns_artifact(artifact_id, user_id, db)
    return _accessible_live_id(artifact_id, user_id, db) or artifact_id
