"""Durable record helpers for process-local presence state."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from infra.database import write_transaction
from infra.presence_time import iso, parse


Span = tuple[datetime, datetime]
Focus = tuple[str | None, str | None]  # (focus_spans, focus_since) as stored


def encode_spans(spans: list[Span]) -> str:
    """Render closed focus spans as the stored JSON array of ``iso`` pairs."""
    return json.dumps([[iso(start), iso(end)] for start, end in spans], separators=(",", ":"))


def decode_spans(raw: str | None) -> list[Span]:
    return [(parse(start), parse(end)) for start, end in json.loads(raw)] if raw else []


def _clipped(spans: list[Span], open_since: datetime | None, entered: datetime, exited: datetime) -> list[Span]:
    """The visit's spans, the open one ended at the exit, kept inside the visit."""
    every = [*spans, *([(open_since, exited)] if open_since is not None else [])]
    kept = [(max(start, entered), min(end, exited)) for start, end in every]
    return [(start, end) for start, end in kept if start < end]


def row(db: sqlite3.Connection, user_id: str, thread_id: str):
    return db.execute(
        "SELECT * FROM thread_presence WHERE user_id=? AND thread_id=?",
        (user_id, thread_id),
    ).fetchone()


def enter(
    db: sqlite3.Connection, user_id: str, thread_id: str, now: datetime, focused: bool = True,
) -> Focus:
    """Open a visit, or continue one whose exit has not been finalized.

    A fresh visit starts with no spans and an open one when the claim is focused
    or unreported; a continuing visit keeps its focus columns, and a NULL pair
    stays NULL. Returns the focus columns the visit now holds.
    """
    stamp = iso(now)
    fresh: Focus = ("[]", stamp if focused else None)
    with write_transaction(db, scope="presence_enter"):
        current = row(db, user_id, thread_id)
        if current is None:
            db.execute(
                "INSERT INTO thread_presence (user_id, thread_id, present, "
                "last_entered_at, last_confirmed_at, updated_at, focus_spans, focus_since) "
                "VALUES (?, ?, 1, ?, ?, ?, ?, ?)",
                (user_id, thread_id, stamp, stamp, stamp, *fresh),
            )
            return fresh
        if current["present"]:
            db.execute(
                "UPDATE thread_presence SET exit_pending_at=NULL, exit_pending_since=NULL, "
                "exit_pending_reason=NULL, marked_read_at=NULL, marked_unread_at=NULL, "
                "last_confirmed_at=?, updated_at=? WHERE user_id=? AND thread_id=?",
                (stamp, stamp, user_id, thread_id),
            )
            return current["focus_spans"], current["focus_since"]
        away = now - parse(current["last_exited_at"])
        turns = db.execute(
            "SELECT COUNT(*) FROM active_turns WHERE thread_id=? AND turn_order > ?",
            (thread_id, current["turn_order_at_exit"] or 0),
        ).fetchone()[0]
        db.execute(
            "UPDATE thread_presence SET present=1, previous_entered_at=last_entered_at, "
            "last_entered_at=?, last_confirmed_at=?, away_seconds=?, "
            "turns_while_away=?, marked_read_at=NULL, marked_unread_at=NULL, "
            "updated_at=?, focus_spans=?, focus_since=? WHERE user_id=? AND thread_id=?",
            (stamp, stamp, max(0, int(away.total_seconds())), turns, stamp,
             *fresh, user_id, thread_id),
        )
        return fresh


def write_focus(
    db: sqlite3.Connection, user_id: str, thread_id: str, focus: Focus, now: datetime,
) -> None:
    """Write the visit's full span list and open span; a focus change confirms presence.

    Always the whole list, never a delta, so any later write repairs a lost one.
    A visit whose spans are NULL predates focus tracking and is left alone.
    """
    with write_transaction(db, scope="presence_focus"):
        db.execute(
            "UPDATE thread_presence SET focus_spans=?, focus_since=?, "
            "last_confirmed_at=MAX(last_confirmed_at, ?), updated_at=? "
            "WHERE user_id=? AND thread_id=? AND present=1 AND focus_spans IS NOT NULL",
            (*focus, iso(now), iso(now), user_id, thread_id),
        )


def close_open_focus_at_restart(db: sqlite3.Connection) -> None:
    """After a restart no focus survives: close each open span at the visit's exit stamp.

    Runs inside the reconcile transaction. The stamp is the exit already pending
    or the last confirmation, so the time the server was down never counts.
    """
    for current in db.execute(
        "SELECT user_id, thread_id, focus_spans, focus_since, "
        "COALESCE(exit_pending_at, last_confirmed_at) AS stamp "
        "FROM thread_presence WHERE present=1 AND focus_spans IS NOT NULL "
        "AND focus_since IS NOT NULL",
    ).fetchall():
        spans = decode_spans(current["focus_spans"])
        since, stamp = parse(current["focus_since"]), parse(current["stamp"])
        if since < stamp:
            spans.append((since, stamp))
        db.execute(
            "UPDATE thread_presence SET focus_spans=?, focus_since=NULL "
            "WHERE user_id=? AND thread_id=?",
            (encode_spans(spans), current["user_id"], current["thread_id"]),
        )


def mark_exit_pending(
    db: sqlite3.Connection, user_id: str, thread_id: str, left_at: datetime,
    reason: str, now: datetime, focus: Focus | None = None,
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
        if focus is not None and current["focus_spans"] is not None:
            db.execute(
                "UPDATE thread_presence SET focus_spans=?, focus_since=? "
                "WHERE user_id=? AND thread_id=?",
                (*focus, user_id, thread_id),
            )
    return True


def finalize(
    db: sqlite3.Connection, current, sessions: int, now: datetime, surface: str,
    focus: Focus | None = None,
) -> None:
    """End one visit and append its history row in the same transaction.

    The focus spans (``focus`` when memory is ahead of the row, else the row's)
    are closed at the exit and clipped to the visit; a NULL pair stays NULL.
    """
    user_id, thread_id = current["user_id"], current["thread_id"]
    entered = parse(current["last_entered_at"])
    exited = max(parse(current["exit_pending_at"]), entered)
    spans, since = focus if focus is not None else (current["focus_spans"], current["focus_since"])
    history_spans = (None if spans is None else encode_spans(_clipped(
        decode_spans(spans), parse(since) if since else None, entered, exited)))
    with write_transaction(db, scope="presence_finalize"):
        project = db.execute(
            "SELECT project_id FROM threads WHERE id=?", (thread_id,),
        ).fetchone()
        db.execute(
            "UPDATE thread_presence SET present=0, last_exited_at=?, "
            "turn_order_at_exit=(SELECT COALESCE(MAX(turn_order), 0) "
            "FROM active_turns WHERE thread_id=?), exit_pending_at=NULL, "
            "exit_pending_since=NULL, exit_pending_reason=NULL, updated_at=?, "
            "focus_spans=NULL, focus_since=NULL "
            "WHERE user_id=? AND thread_id=?",
            (iso(exited), thread_id, iso(now), user_id, thread_id),
        )
        db.execute(
            "INSERT INTO presence_visits (user_id, surface, thread_id, project_id, "
            "entered_at, exited_at, seconds, sessions, exit_reason, focus_spans) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, surface, thread_id, project["project_id"] if project else None,
             iso(entered), iso(exited), int((exited - entered).total_seconds()),
             max(1, sessions), current["exit_pending_reason"] or "disconnected", history_spans),
        )


def keep_alive(
    db: sqlite3.Connection, live: list[tuple[str, str]], now: datetime, interval: timedelta,
) -> None:
    """Refresh ``last_confirmed_at`` of each live (user, thread) visit confirmed longer ago than ``interval``."""
    if not live:
        return
    stale = iso(now - interval)
    with write_transaction(db, scope="presence_keep_alive"):
        for user_id, thread_id in live:
            db.execute(
                "UPDATE thread_presence SET last_confirmed_at=?, updated_at=? "
                "WHERE user_id=? AND thread_id=? AND present=1 "
                "AND last_confirmed_at <= ?",
                (iso(now), iso(now), user_id, thread_id, stale),
            )


def mark_restart_exits(db: sqlite3.Connection, now: datetime) -> None:
    """After a restart every open visit becomes exit-pending and its open focus span closes.

    The exit is stamped at the last confirmation (or the exit already pending), and the
    merge window restarts now, so a browser that reconnects within 2 min continues the visit.
    """
    with write_transaction(db, scope="presence_reconcile"):
        close_open_focus_at_restart(db)
        db.execute(
            "UPDATE thread_presence SET "
            "exit_pending_at=COALESCE(exit_pending_at, last_confirmed_at), "
            "exit_pending_since=?, "
            "exit_pending_reason=COALESCE(exit_pending_reason, 'restart'), "
            "updated_at=? WHERE present=1",
            (iso(now), iso(now)),
        )


def mark_seen(
    db: sqlite3.Connection, *, user_id: str, thread_id: str, unread: bool, now: datetime,
) -> bool:
    """Mark read through ``now`` or unread; False with no record, never a new visit."""
    stamp = iso(now)
    with write_transaction(db, scope="presence_mark_seen"):
        return db.execute(
            "UPDATE thread_presence SET marked_read_at=CASE WHEN ? THEN marked_read_at ELSE ? END, "
            "marked_unread_at=CASE WHEN ? THEN ? END, updated_at=? WHERE user_id=? AND thread_id=?",
            (unread, stamp, unread, stamp, stamp, user_id, thread_id),
        ).rowcount == 1
