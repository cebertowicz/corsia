"""One document's comment state follows it when a promote cannot carry its identity.

A promote that cannot carry the working file's identity (#2555) registers a new identity and
tombstones the old one. The person's comments must not stay on the tombstone. The rule is
plain: each owner module moves its own rows to the successor identity under its own key, and
nothing is merged -- a comment id, an image region number, a writer's place in line and a
batch's due time keep the values the person has already seen. The successor is an identity
registered for the promoted file in that same transaction, so it holds no comment state of
its own; `refuse_if_occupied` proves that before a row moves rather than inventing merge rules
for a state no promote can produce. `registry.models.retire` calls this inside its own write
transaction, so the tombstone, the successor link and every moved row commit together or not
at all.
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
    """Stop a promote whose successor already holds comment state, with what to do about it.

    Two documents' comment state is never merged here: positions the person has read would
    change, and every merge rule tried has cost more than the rare case it served. A promote
    registers its target identity in the same transaction, so an occupied successor means the
    shared path was already registered to a document with comments of its own while the working
    file's recorded hash had gone stale. The caller's transaction rolls the promote back, so the
    file, its comments and both documents stay exactly as they were.
    """
    occupied = occupied_comment_tables(db, to_artifact_id)
    if not occupied:
        return
    target = db.execute(
        "SELECT path FROM artifact_identities WHERE id=?", (to_artifact_id,)
    ).fetchone()
    where = f" at {target[0]}" if target and target[0] else ""
    raise SuccessorHoldsComments(
        f"Cannot promote: the shared path is still registered to an earlier document{where} "
        f"({to_artifact_id}) that holds {', '.join(occupied)}, and this file's recorded hash "
        "no longer matches its bytes, so the promote cannot keep its own identity either. "
        "Nothing was moved and the promote is rolled back. Promote to a different shared "
        "path, or re-register the working file so its recorded hash matches its bytes first."
    )


def move_to_artifact(db, *, from_artifact_id: str, to_artifact_id: str) -> int:
    """Move every row of comment state to the successor identity; returns how many moved.

    Called by `registry.models.retire` for the identity a promoted document now lives under.
    Refuses first if the successor is occupied. Comment ids never change, so replies, tags,
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
