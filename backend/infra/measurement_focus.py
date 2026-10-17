"""Durable focused intervals used only by Measurement attention."""

from __future__ import annotations

import functools
import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import ParamSpec

from infra.database import write_transaction
from infra.presence_time import iso

log = logging.getLogger(__name__)

_P = ParamSpec("_P")


def _fail_soft(write: Callable[_P, None]) -> Callable[_P, None]:
    """Keep a failed Measurement write from reaching shared presence.

    Focus intervals only feed Measurement attention; the presence claim, release
    and restart paths that call these writes own the shared state and must finish
    whether or not the interval write could run (a lock, a full disk). A lost
    write costs that interval, never a stuck visit.
    """
    @functools.wraps(write)
    def run(*args: _P.args, **kwargs: _P.kwargs) -> None:
        try:
            write(*args, **kwargs)
        except sqlite3.Error:
            log.warning("measurement focus write %s failed; attention loses this interval",
                        write.__name__, exc_info=True)
    return run


@_fail_soft
def set_focused(
    db: sqlite3.Connection, *, user_id: str, surface: str, thread_id: str,
    session: str, focused: bool, now: datetime,
) -> None:
    """Open or close this session's Measurement interval without changing presence."""
    stamp = iso(now)
    with write_transaction(db, scope="measurement_focus"):
        db.execute(
            "INSERT OR IGNORE INTO measurement_focus_users (user_id, focused_since) VALUES (?, ?)",
            (user_id, stamp),
        )
        if focused:
            db.execute(
                "INSERT INTO measurement_focus_intervals "
                "(user_id, surface, thread_id, session_id, focused_at) "
                "SELECT ?, ?, ?, ?, ? WHERE NOT EXISTS ("
                "SELECT 1 FROM measurement_focus_intervals "
                "WHERE session_id=? AND surface=? AND thread_id=? AND blurred_at IS NULL)",
                (user_id, surface, thread_id, session, stamp, session, surface, thread_id),
            )
            return
        db.execute(
            "UPDATE measurement_focus_intervals SET blurred_at=MAX(focused_at, ?) "
            "WHERE session_id=? AND surface=? AND thread_id=? AND blurred_at IS NULL",
            (stamp, session, surface, thread_id),
        )


@_fail_soft
def end_session(db: sqlite3.Connection, *, session: str, surface: str, now: datetime) -> None:
    """Close any focus interval when its underlying presence claim leaves.

    ``now`` may be back-dated (an idle release); an interval opened after it
    closes at its own start rather than before it.
    """
    with write_transaction(db, scope="measurement_focus_end"):
        db.execute(
            "UPDATE measurement_focus_intervals SET blurred_at=MAX(focused_at, ?) "
            "WHERE session_id=? AND surface=? AND blurred_at IS NULL",
            (iso(now), session, surface),
        )


@_fail_soft
def reconcile_startup(db: sqlite3.Connection, *, now: datetime) -> None:
    """No focus claim survives a server restart."""
    with write_transaction(db, scope="measurement_focus_reconcile"):
        db.execute(
            "UPDATE measurement_focus_intervals SET blurred_at=? WHERE blurred_at IS NULL",
            (iso(now),),
        )


def delete_thread(db: sqlite3.Connection, thread_id: str) -> None:
    """Remove Measurement intervals with their thread's other presence state.

    Not fail-soft: it runs in the thread delete's own transaction, and a row left
    behind would still reference the deleted thread.
    """
    db.execute("DELETE FROM measurement_focus_intervals WHERE thread_id=?", (thread_id,))
