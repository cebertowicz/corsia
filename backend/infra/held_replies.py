"""Held replies: a Local only agent's message to a cloud-bound recipient, stored until the person decides.

The row keeps the exact bytes that approval delivers. ``plans.held_attention.pending`` is the one
reader of what waits on the person.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from infra.database import write_transaction
from infra.time import utc_now_iso

HELD = "held"
APPROVED = "approved"
SENT = "sent"
DECLINED = "declined"
NOT_SENT = "not_sent"


def recipient_label(title: str | None) -> str:
    """The recipient as the person reads it: a name that doesn't say it is an agent gets "the … agent"."""
    name = (title or "").strip()
    if not name:
        return "the recipient"
    if "agent" in name.lower() or "conductor" in name.lower():
        return name
    return f"the {name} agent"



def hold(
    *, user_id: str, sender_thread_id: str, recipient_thread_id: str, text: str, via: str,
    plan_node_id: str | None = None, idempotency_key: str | None = None,
    intent: str = "progress", child_turn_id: str | None = None,
    db: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Store one message for the person's decision; a retry with the same key returns the first row."""
    key = (idempotency_key or "").strip() or None
    with write_transaction(db, scope="hold_reply") as connection:
        if key is not None:
            existing = connection.execute(
                "SELECT * FROM held_replies WHERE sender_thread_id=? AND recipient_thread_id=? "
                "AND idempotency_key=?",
                (sender_thread_id, recipient_thread_id, key),
            ).fetchone()
            if existing is not None:
                return {**dict(existing), "duplicate": True}
        row = {
            "id": str(uuid.uuid4()), "user_id": user_id, "sender_thread_id": sender_thread_id,
            "recipient_thread_id": recipient_thread_id, "text": text, "via": via,
            "plan_node_id": plan_node_id, "intent": intent, "child_turn_id": child_turn_id,
            "idempotency_key": key, "state": HELD, "error": None,
            "created_at": utc_now_iso(), "decided_at": None,
        }
        connection.execute(
            f"INSERT INTO held_replies ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
            tuple(row.values()),
        )
    return {**row, "duplicate": False}


def get(held_id: str, user_id: str, *, db: sqlite3.Connection) -> dict[str, Any] | None:
    """One of the user's held replies, in any state."""
    row = db.execute(
        "SELECT * FROM held_replies WHERE id=? AND user_id=?", (held_id, user_id),
    ).fetchone()
    return None if row is None else dict(row)


def recover_interrupted() -> None:
    """Return approvals interrupted by a restart to the person's attention."""
    from infra.held_reply_messages import RESTART_ERROR

    with write_transaction(None, scope="recover_held_replies") as connection:
        connection.execute(
            "UPDATE held_replies SET state=?, error=?, decided_at=NULL WHERE state=?",
            (HELD, RESTART_ERROR, APPROVED),
        )


def settle(
    held_id: str, *, expect: str, state: str, error: str | None = None,
    db: sqlite3.Connection | None = None,
) -> bool:
    """Move one row from ``expect`` to ``state``; False when another decision got there first."""
    with write_transaction(db, scope="settle_held_reply") as connection:
        moved = connection.execute(
            "UPDATE held_replies SET state=?, error=?, "
            "text=CASE WHEN ? IN ('sent','declined','not_sent') THEN '' ELSE text END, "
            "decided_at=CASE WHEN ?='held' THEN NULL ELSE ? END WHERE id=? AND state=?",
            (state, error, state, state, utc_now_iso(), held_id, expect),
        )
    return moved.rowcount == 1
