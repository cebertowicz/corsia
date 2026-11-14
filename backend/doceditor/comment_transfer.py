"""One document's comment state follows it when a promote cannot carry its identity.

A promote that cannot carry the working file's identity (#2555) registers a new identity and
tombstones the old one. The person's comments must not stay on the tombstone. The rule is
plain: each owner module moves its own rows to the successor identity under its own key, and
nothing is merged -- a comment id, an image region number, a writer's place in line and a
batch's due time keep the values the person has already seen. Two documents that both hold
comment state cannot be joined without changing what the person has read, so
`refuse_if_occupied` stops that promote before a row moves, rather than inventing merge rules
for it. A file with no comment state moves nothing and checks nothing, so updating a shared
document that has comments behaves as it always did. `registry.models.retire` calls this inside
its own write transaction, so the tombstone, the successor link and every moved row commit
together or not at all.
"""

from __future__ import annotations

from doceditor import comments, feedback_batch_core, review_requests, writer_line_move
from registry.shared_write_errors import SuccessorHoldsComments

# Each module moves the rows it owns: `comments` the threads and their read marks,
# `writer_line_move` the agents' place in line, `feedback_batch_core` the batches and their
# items, `review_requests` the asks of one reviewer thread.
OWNERS = (
    ("comments", comments),
    ("writer lines", writer_line_move),
    ("feedback batches", feedback_batch_core),
    ("review requests", review_requests),
)


def occupied_comment_tables(db, artifact_id: str) -> list[str]:
    """Which kinds of comment state this identity holds; empty for a freshly registered one."""
    return [
        label
        for label, owner in OWNERS
        if owner.holds_rows(db, artifact_id)
    ]


def refuse_if_occupied(db, *, from_artifact_id: str, to_artifact_id: str) -> None:
    """Stop a promote when the file and the document at the shared path both hold comment state.

    Two documents' comment state is never merged here: positions the person has read would
    change, and every merge rule tried has cost more than the rare case it served. A file with
    no comment state of its own has nothing to merge, so it passes. The caller's transaction
    rolls the promote back, so the file, its comments and both documents stay exactly as they
    were.
    """
    if not occupied_comment_tables(db, from_artifact_id):
        return
    if not occupied_comment_tables(db, to_artifact_id):
        return
    target = db.execute(
        "SELECT path FROM artifact_identities WHERE id=?", (to_artifact_id,)
    ).fetchone()
    where = f" at {target[0]}" if target and target[0] else ""
    raise SuccessorHoldsComments(
        f"Couldn’t promote: this file and the document already registered{where} both have "
        "comments, and comment threads from two documents can’t be merged. Nothing was moved and "
        "the promote is rolled back. Promote to a different shared path."
    )


def move_to_artifact(db, *, from_artifact_id: str, to_artifact_id: str) -> int:
    """Move every row of comment state to the successor identity; returns how many moved.

    Called by `registry.models.retire` for the identity a promoted document now lives under.
    Returns 0 and moves nothing when the file holds no comment state; refuses first when the
    successor holds some too. Comment ids never change, so replies, tags,
    mentions, recipient state, acknowledgements and thread read marks follow their threads by
    themselves, and anchors stay valid because the promoted bytes are the file's own.
    """
    refuse_if_occupied(db, from_artifact_id=from_artifact_id, to_artifact_id=to_artifact_id)
    moved = 0
    for _label, owner in OWNERS:
        moved += owner.move_identity(
            db, from_artifact_id=from_artifact_id, to_artifact_id=to_artifact_id
        )
    return moved
