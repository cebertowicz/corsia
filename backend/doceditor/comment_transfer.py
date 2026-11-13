"""Carry a document's comment state to the identity it moved to.

A promote that cannot keep a working file's identity registers a new one and retires the old, and
the person's comments must not stay behind on the retired id. So ``registry.models.retire``, having
named the successor, calls this in that same transaction: the move, the tombstone and the new
registration commit or roll back together, and no caller can name a successor and strand a thread.

This module deliberately holds no rules of its own. Every row here belongs to another module, and
each of them moves its own rows with its own invariants -- comment ids and image-region numbering
(``comments.move_identity``), a queued writer line's place in line and its wake
(``writer_line_move.move_identity``), a feedback batch's items, coalesced edit and count
(``feedback_batch_core.move_identity``), a review request's state (``review_requests.move_identity``).
An owner's next invariant then needs an edit in that owner only, not a matching copy here.
"""

from __future__ import annotations

from doceditor import comments, feedback_batch_core, review_requests, writer_line_move


def move_to_artifact(db, *, source_artifact_id: str, target_artifact_id: str) -> int:
    """Move every comment row of a retiring identity onto the identity that replaces it; returns how many.

    Runs in the caller's write transaction. Never called with the same id on both sides -- a promote
    that carries the identity never retires it -- and a no-op then anyway. The order is the
    dependency order: comments first, since a writer line's thread root and a batch's items name
    them, then the writer lines, the batches and the review requests.
    """
    if not source_artifact_id or not target_artifact_id or target_artifact_id == source_artifact_id:
        return 0
    moved = comments.move_identity(db, from_artifact_id=source_artifact_id, to_artifact_id=target_artifact_id)
    moved += writer_line_move.move_identity(
        db, from_artifact_id=source_artifact_id, to_artifact_id=target_artifact_id,
    )
    moved += feedback_batch_core.move_identity(
        db, from_artifact_id=source_artifact_id, to_artifact_id=target_artifact_id,
    )
    return moved + review_requests.move_identity(
        db, from_artifact_id=source_artifact_id, to_artifact_id=target_artifact_id,
    )
