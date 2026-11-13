"""Lane names, deadlines and open-batch writes shared by every feedback-batch module."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

import infra.sweep_wake as sweep_wake
from infra.write_effects import after_commit

AGENT_QUIET = 120
USER_IDLE = 120
IMMEDIATE_IDLE = 5
# A person's typing pauses mid-thought for 5-15 s; a separate edit comes a minute or more later.
EDIT_IDLE = 30
IMMEDIATE = "immediate"
DOCUMENT_HELD = "document-held"
IMAGE_HELD = "image-held"
HELD_POLICIES = (DOCUMENT_HELD, IMAGE_HELD)
# The lanes a person may name for a document comment; an image-region comment is forced into IMAGE_HELD server-side.
# Typing owns the one spelling (a Literal cannot name the constants above), so requests and handlers import it from here.
PersonDocumentLane = Literal["immediate", "document-held"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def person_comment_lane(requested: str, *, region: bool) -> str:
    """The lane a person's new comment or re-open queues in: an image-region comment is always image-held, whatever lane was asked for."""
    return IMAGE_HELD if region else requested


def _after(value: str, seconds: int) -> str:
    return (datetime.fromisoformat(value) + timedelta(seconds=seconds)).isoformat()


def new_deadline(author_kind: str, delivery_policy: str, now: str, *, edit: bool = False) -> str:
    """When a batch closes: an immediate comment goes after a short idle, a person's edit once they pause typing."""
    if author_kind == "agent":
        return _after(now, AGENT_QUIET)
    if delivery_policy != IMMEDIATE:
        return _after(now, USER_IDLE)
    return _after(now, EDIT_IDLE if edit else IMMEDIATE_IDLE)


def _validate_policy(author_kind: str, delivery_policy: str) -> str:
    if author_kind == "agent":
        return IMMEDIATE
    if delivery_policy not in {IMMEDIATE, DOCUMENT_HELD, IMAGE_HELD}:
        raise ValueError("invalid user feedback delivery policy")
    return delivery_policy


def open_batch(
    db,
    *,
    author_kind: str,
    author_id: str,
    artifact_id: str,
    recipient_id: str,
    delivery_policy: str,
    now: str,
) -> str:
    """Return the canonical open batch for one author, artifact, recipient, and lane."""
    delivery_policy = _validate_policy(author_kind, delivery_policy)
    row = db.execute(
        """SELECT id FROM doc_feedback_batches
           WHERE author_kind=? AND author_id=? AND artifact_id=?
             AND recipient_id=? AND delivery_policy=? AND state='open'""",
        (author_kind, author_id, artifact_id, recipient_id, delivery_policy),
    ).fetchone()
    if row is not None:
        return str(row["id"])
    batch_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO doc_feedback_batches
           (id,artifact_id,author_kind,author_id,recipient_id,delivery_policy,state,
            opened_at,last_item_at,closes_at,item_count,created_at,updated_at)
           VALUES (?,?,?,?,?,?,'open',?,?,?,0,?,?)""",
        (
            batch_id, artifact_id, author_kind, author_id, recipient_id, delivery_policy,
            now, now, new_deadline(author_kind, delivery_policy, now), now, now,
        ),
    )
    # A new deadline can be earlier than the one the continuation loop is sleeping toward.
    after_commit(db, sweep_wake.sweep_soon)
    return batch_id


def touch_batch(
    db,
    batch_id: str,
    *,
    author_kind: str,
    delivery_policy: str,
    now: str,
    item_delta: int,
    edit: bool = False,
) -> None:
    db.execute(
        """UPDATE doc_feedback_batches
           SET last_item_at=?,closes_at=?,closed_reason=NULL,
               item_count=item_count+?,updated_at=? WHERE id=?""",
        (now, new_deadline(author_kind, delivery_policy, now, edit=edit), item_delta, now, batch_id)
    )


def add_comment_item(
    db,
    *,
    batch_id: str,
    comment_id: str,
    author_kind: str,
    delivery_policy: str,
    now: str,
    event: str | None = None,
    event_by: str | None = None,
) -> bool:
    """Add one comment item once and keep the batch deadline/count in sync."""
    inserted = db.execute(
        """INSERT OR IGNORE INTO doc_feedback_batch_items
           (batch_id,comment_id,event,event_at,event_by) VALUES (?,?,?,?,?)""",
        (batch_id, comment_id, event, now if event else None, event_by),
    )
    if not inserted.rowcount:
        return False
    touch_batch(
        db, batch_id, author_kind=author_kind, delivery_policy=delivery_policy, now=now, item_delta=1,
    )
    return True


def count_items(db, batch_id: str) -> int:
    """How many items a batch holds: its comment items plus its one coalesced edit item.

    ``feedback_reopens.discard_open_item`` counts the same two sets when it decides whether a batch
    still has anything to deliver; a batch is never emptied by deleting one of them alone.
    """
    return db.execute(
        "SELECT COUNT(*) FROM doc_feedback_batch_items WHERE batch_id=?", (batch_id,)
    ).fetchone()[0] + db.execute(
        "SELECT COUNT(*) FROM doc_feedback_edit_items WHERE batch_id=?", (batch_id,)
    ).fetchone()[0]


def _carry_edit_item(db, *, from_batch_id: str, into_batch_id: str) -> None:
    """Move a batch's coalesced edit item, or fold it into the one the other batch already holds.

    The row cascade-deletes with its batch, so it is always carried before a batch is deleted. Where
    both sides have one, the joined batch keeps the baseline and the diff it already had -- that is
    what the recipient has not seen yet -- and takes the later saved text, the summed save count and
    the later time, so the newest text is never the one lost.
    """
    moving = db.execute(
        "SELECT * FROM doc_feedback_edit_items WHERE batch_id=?", (from_batch_id,)
    ).fetchone()
    if moving is None:
        return
    held = db.execute(
        "SELECT * FROM doc_feedback_edit_items WHERE batch_id=?", (into_batch_id,)
    ).fetchone()
    if held is None:
        db.execute(
            "UPDATE doc_feedback_edit_items SET batch_id=? WHERE batch_id=?", (into_batch_id, from_batch_id)
        )
        return
    later_is_newer = str(moving["last_at"]) > str(held["last_at"])
    newest = moving if later_is_newer else held
    db.execute(
        """UPDATE doc_feedback_edit_items
           SET latest_sha256=?, latest_text=?, save_count=?, last_at=? WHERE batch_id=?""",
        (
            newest["latest_sha256"], newest["latest_text"],
            int(moving["save_count"]) + int(held["save_count"]),
            max(str(moving["last_at"]), str(held["last_at"])), into_batch_id,
        ),
    )
    db.execute("DELETE FROM doc_feedback_edit_items WHERE batch_id=?", (from_batch_id,))


def move_identity(db, *, from_artifact_id: str, to_artifact_id: str) -> int:
    """Carry this document's feedback batches to the identity that replaces it; returns how many.

    One open batch per author, document, recipient and lane is the only key two documents share
    (``idx_doc_feedback_batches_open``), so the batch that is already open on the target stays the
    recipient's one batch for that author: the moved batch's comment items and its edit item join
    it, its count is retaken from what the batch now holds, and the earlier closing time is kept so
    nothing that was due sooner is made to wait. Any other batch just changes document.
    """
    moved = 0
    for source in db.execute(
        "SELECT * FROM doc_feedback_batches WHERE artifact_id=?", (from_artifact_id,)
    ).fetchall():
        open_target = db.execute(
            """SELECT * FROM doc_feedback_batches
               WHERE artifact_id=? AND state='open' AND author_kind=? AND author_id=?
                 AND recipient_id=? AND delivery_policy=?""",
            (to_artifact_id, source["author_kind"], source["author_id"], source["recipient_id"],
             source["delivery_policy"]),
        ).fetchone() if str(source["state"]) == "open" else None
        if open_target is None:
            db.execute(
                "UPDATE doc_feedback_batches SET artifact_id=? WHERE id=?", (to_artifact_id, source["id"])
            )
        else:
            db.execute(
                """INSERT OR IGNORE INTO doc_feedback_batch_items(batch_id, comment_id, event, event_at, event_by)
                   SELECT ?, comment_id, event, event_at, event_by FROM doc_feedback_batch_items
                   WHERE batch_id=?""",
                (open_target["id"], source["id"]),
            )
            _carry_edit_item(db, from_batch_id=str(source["id"]), into_batch_id=str(open_target["id"]))
            db.execute(
                """UPDATE doc_feedback_batches SET closes_at=?, item_count=?, updated_at=? WHERE id=?""",
                (min(str(source["closes_at"]), str(open_target["closes_at"])),
                 count_items(db, str(open_target["id"])), utc_now(), open_target["id"]),
            )
            db.execute("DELETE FROM doc_feedback_batches WHERE id=?", (source["id"],))
        moved += 1
    return moved
