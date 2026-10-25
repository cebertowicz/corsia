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
IMMEDIATE = "immediate"
DOCUMENT_HELD = "document-held"
IMAGE_HELD = "image-held"
HELD_POLICIES = (DOCUMENT_HELD, IMAGE_HELD)
# The lanes a person may name for a document comment; an image-region comment is forced into IMAGE_HELD server-side.
# Typing owns the one spelling (a Literal cannot name the constants above), so requests and handlers import it from here.
PersonDocumentLane = Literal["immediate", "document-held"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _after(value: str, seconds: int) -> str:
    return (datetime.fromisoformat(value) + timedelta(seconds=seconds)).isoformat()


def new_deadline(author_kind: str, delivery_policy: str, now: str) -> str:
    if author_kind == "agent":
        return _after(now, AGENT_QUIET)
    return _after(now, IMMEDIATE_IDLE if delivery_policy == IMMEDIATE else USER_IDLE)


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
) -> None:
    db.execute(
        """UPDATE doc_feedback_batches
           SET last_item_at=?,closes_at=?,closed_reason=NULL,
               item_count=item_count+?,updated_at=? WHERE id=?""",
        (now, new_deadline(author_kind, delivery_policy, now), item_delta, now, batch_id)
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
