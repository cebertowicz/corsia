"""Who may use a document's comments, and which document a pane's comment request belongs to.

The comment routes share one ownership gate. A pane still open on a promoted document's retired id
is followed to its live successor: writes land there or are refused with ``NOT_COMMENTABLE``, and the list
a stale pane reads is the live document's comments plus any left on the retired id itself.
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


def _live_artifact_id(artifact_id: str, user_id: str, db) -> str:
    """The artifact a person's comment write belongs to, for a pane that may still hold a retired id.

    A promote retires the working file's identity and carries its comment state to the successor
    (``doceditor.comment_transfer``), so a write keyed by the old id follows it there: ``artifact_id``
    itself unless it was promoted, else the active identity its successor chain ends at. Refuses with
    409 and ``NOT_COMMENTABLE`` when the chain ends nowhere the caller may write, so the browser keeps
    the draft instead of the write landing on a row nobody reads.
    """
    live = registry_models.live_identity(artifact_id, db=db)
    if live is not None and live != artifact_id:
        try:
            _assert_owns_artifact(live, user_id, db)
        except HTTPException:
            live = None
    if live is None:
        raise HTTPException(409, NOT_COMMENTABLE)
    return live


def _artifact_for_write(artifact_id: str, user_id: str, db) -> str:
    """``_assert_owns_artifact`` for a write, returning the artifact to write to (see ``_live_artifact_id``)."""
    _assert_owns_artifact(artifact_id, user_id, db)
    return _live_artifact_id(artifact_id, user_id, db)


def _lineage_of(live: str, user_id: str, db) -> list[str]:
    """``live`` first, then every earlier identity of the document the caller may read, in id order.

    A promote carries a retired identity's comments to its successor, but one can still hold
    comments saved there before the promote carried any; they are the document's comments too.
    Comment ids are unique, so the sets never collide.
    """
    earlier = []
    for identity in sorted(registry_models.predecessors(live, db=db)):
        try:
            _assert_owns_artifact(identity, user_id, db)
        except HTTPException:
            continue
        earlier.append(identity)
    return [live, *earlier]


def _artifacts_for_read(artifact_id: str, user_id: str, db) -> list[str]:
    """The document's lineage as a pane on ``artifact_id`` reads it: the same list from any of its ids.

    When no live document the caller may read exists, the lineage of the id named.
    """
    _assert_owns_artifact(artifact_id, user_id, db)
    live = registry_models.live_identity(artifact_id, db=db)
    if live is not None and live != artifact_id:
        try:
            _assert_owns_artifact(live, user_id, db)
        except HTTPException:
            live = None
    return _lineage_of(live or artifact_id, user_id, db)


def _lineage_for_write(artifact_id: str, user_id: str, db) -> list[str]:
    """The lineage a write acts across (a thread's viewed stamp, dismiss-all): the live id and its earlier ones."""
    return _lineage_of(_artifact_for_write(artifact_id, user_id, db), user_id, db)
