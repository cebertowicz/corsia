"""Durable record helpers for process-local presence state."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from infra.database import write_transaction
from infra.presence_time import iso, parse


def row(db: sqlite3.Connection, user_id: str, thread_id: str):
    return db.execute(
        "SELECT * FROM thread_presence WHERE user_id=? AND thread_id=?",
        (user_id, thread_id),
    ).fetchone()


def enter(db: sqlite3.Connection, user_id: str, thread_id: str, now: datetime) -> None:
    """Open a visit, or continue one whose exit has not been finalized."""
    stamp = iso(now)
    with write_transaction(db, scope="presence_enter"):
        current = row(db, user_id, thread_id)
        if current is None:
            db.execute(
                "INSERT INTO thread_presence (user_id, thread_id, present, "
                "last_entered_at, last_confirmed_at, updated_at) "
                "VALUES (?, ?, 1, ?, ?, ?)",
                (user_id, thread_id, stamp, stamp, stamp),
            )
        elif current["present"]:
            db.execute(
                "UPDATE thread_presence SET exit_pending_at=NULL, exit_pending_since=NULL, "
                "exit_pending_reason=NULL, marked_read_at=NULL, marked_unread_at=NULL, "
                "last_confirmed_at=?, updated_at=? WHERE user_id=? AND thread_id=?",
                (stamp, stamp, user_id, thread_id),
            )
        else:
            away = now - parse(current["last_exited_at"])
            turns = db.execute(
                "SELECT COUNT(*) FROM active_turns WHERE thread_id=? AND turn_order > ?",
                (thread_id, current["turn_order_at_exit"] or 0),
            ).fetchone()[0]
            db.execute(
                "UPDATE thread_presence SET present=1, previous_entered_at=last_entered_at, "
                "last_entered_at=?, last_confirmed_at=?, away_seconds=?, "
                "turns_while_away=?, marked_read_at=NULL, marked_unread_at=NULL, "
                "updated_at=? WHERE user_id=? AND thread_id=?",
                (stamp, stamp, max(0, int(away.total_seconds())), turns, stamp,
                 user_id, thread_id),
            )


def mark_exit_pending(
    db: sqlite3.Connection, user_id: str, thread_id: str, left_at: datetime,
    reason: str, now: datetime,
) -> bool:
    with write_transaction(db, scope="presence_exit_pending"):
        current = row(db, user_id, thread_id)
        if current is None or not current["present"]:
            return False
        stamp = max(left_at, parse(current["last_entered_at"]))
        db.execute(
            "UPDATE thread_presence SET exit_pending_at=?, exit_pending_since=?, "
            "exit_pending_reason=?, updated_at=? WHERE user_id=? AND thread_id=?",
            (iso(stamp), iso(now), reason, iso(now), user_id, thread_id),
        )
    return True


def finalize(db: sqlite3.Connection, current, sessions: int, now: datetime, surface: str) -> None:
    """End one visit and append its history row in the same transaction."""
    user_id, thread_id = current["user_id"], current["thread_id"]
    entered = parse(current["last_entered_at"])
    exited = max(parse(current["exit_pending_at"]), entered)
    with write_transaction(db, scope="presence_finalize"):
        project = db.execute(
            "SELECT project_id FROM threads WHERE id=?", (thread_id,),
        ).fetchone()
        db.execute(
            "UPDATE thread_presence SET present=0, last_exited_at=?, "
            "turn_order_at_exit=(SELECT COALESCE(MAX(turn_order), 0) "
            "FROM active_turns WHERE thread_id=?), exit_pending_at=NULL, "
            "exit_pending_since=NULL, exit_pending_reason=NULL, updated_at=? "
            "WHERE user_id=? AND thread_id=?",
            (iso(exited), thread_id, iso(now), user_id, thread_id),
        )
        db.execute(
            "INSERT INTO presence_visits (user_id, surface, thread_id, project_id, "
            "entered_at, exited_at, seconds, sessions, exit_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, surface, thread_id, project["project_id"] if project else None,
             iso(entered), iso(exited), int((exited - entered).total_seconds()),
             max(1, sessions), current["exit_pending_reason"] or "disconnected"),
        )
