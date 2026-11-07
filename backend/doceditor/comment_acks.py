"""Who has acknowledged a comment thread: one row per thread and acknowledger.

Each person and agent acknowledges a thread separately. The person is ``PERSON`` (the principal
their comments are delivered as); an agent is its thread id. ``comment_state_updates`` records
an acknowledgement and resolves the thread once everyone in it has one
(``feedback_comments.participants`` says who is in it). Two state changes take acknowledgements
back: re-opening the thread clears them all (``comments.update``), and a tag that brings an agent
back clears its own (``comment_recipients.reinstate``). An acknowledged agent gets no more wakes for
the thread (``feedback_comments.recipients``, ``comment_recipients.filter_stopped_replies``); the
person's acknowledgement never stops replies reaching them.
"""

from __future__ import annotations

from datetime import datetime, timezone

from doceditor.feedback_state import USER_AUTHOR_ID

PERSON = USER_AUTHOR_ID


def record(db, root_id: str, actor_id: str, name: str, acknowledged: bool) -> bool:
    """Add or take back one acknowledgement; True when that changed anything."""
    if acknowledged:
        cursor = db.execute(
            """INSERT INTO doc_comment_acks(root_id,actor_id,actor_name,acked_at) VALUES (?,?,?,?)
               ON CONFLICT(root_id,actor_id) DO NOTHING""",
            (root_id, actor_id, name, datetime.now(timezone.utc).isoformat()),
        )
    else:
        cursor = db.execute("DELETE FROM doc_comment_acks WHERE root_id=? AND actor_id=?", (root_id, actor_id))
    return cursor.rowcount > 0


def clear(db, root_id: str, actor_id: str | None = None) -> bool:
    """Take back every acknowledgement on the thread, or only ``actor_id``'s; True when any was taken back."""
    if actor_id is None:
        cursor = db.execute("DELETE FROM doc_comment_acks WHERE root_id=?", (root_id,))
    else:
        cursor = db.execute("DELETE FROM doc_comment_acks WHERE root_id=? AND actor_id=?", (root_id, actor_id))
    return cursor.rowcount > 0


def acknowledgers(db, root_id: str) -> set[str]:
    return {str(row["actor_id"]) for row in db.execute(
        "SELECT actor_id FROM doc_comment_acks WHERE root_id=?", (root_id,))}


def attach(db, rows: list[dict]) -> None:
    """Give each top-level row ``acknowledged_by``, in the order they acknowledged, in one read."""
    roots = {row["id"]: row for row in rows if not row.get("parent_id")}
    for row in roots.values():
        row["acknowledged_by"] = []
    if not roots:
        return
    marks = ",".join("?" for _ in roots)
    for ack in db.execute(
        f"SELECT root_id,actor_id,actor_name FROM doc_comment_acks WHERE root_id IN ({marks}) ORDER BY acked_at,actor_id",
        tuple(roots),
    ):
        kind = "user" if ack["actor_id"] == PERSON else "agent"
        roots[ack["root_id"]]["acknowledged_by"].append({"id": ack["actor_id"], "name": ack["actor_name"], "kind": kind})
