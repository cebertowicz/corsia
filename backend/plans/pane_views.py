"""What the user has seen of a thread's plan in the plan pane.

When the user opens a plan, the only finished rows still showing are the ones
that finished since they last looked at it there; after a pause they fold
into their level's count. Opening the conversation is not seeing the plan:
only the pane, or the mobile plan drawer, showing the freshly loaded plan in
a visible page reports a view. Design:
``shared/planning/plans-fold-since-last-look-2026-09-25.md``.

One watermark per (user, thread), ``seen_through``: every node settled at or
before it has been on screen. The client reports the ``as_of`` of the plan it
showed. It only ever moves forward, and the server clamps it to its own
clock, so a skewed client can never mark the future seen.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from infra.database import write_transaction


class PaneViewInputError(ValueError):
    """A reported watermark is not a timestamp with a zone."""


def _instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise PaneViewInputError("seen_through must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PaneViewInputError("seen_through must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise PaneViewInputError("seen_through must carry a time zone")
    return parsed.astimezone(UTC)


def _stored(moment: datetime) -> str:
    # One fixed width, so stored values also compare correctly as text.
    return moment.isoformat(timespec="microseconds")


def read(db: sqlite3.Connection, *, user_id: str, thread_id: str) -> str | None:
    """The user's watermark for this thread; None before their first view."""
    row = db.execute(
        "SELECT seen_through FROM plan_pane_views WHERE user_id=? AND thread_id=?",
        (user_id, thread_id),
    ).fetchone()
    return None if row is None else row[0]


def delete_thread(db: sqlite3.Connection, thread_id: str) -> None:
    """Forget every reader's mark for a thread that is being deleted.

    Runs inside the caller's transaction, beside the presence teardown, so no
    mark outlives the thread row it references.
    """
    db.execute("DELETE FROM plan_pane_views WHERE thread_id=?", (thread_id,))


def record(
    db: sqlite3.Connection, *, user_id: str, thread_id: str, seen_through: object,
) -> dict:
    """Advance the user's watermark for this thread and return what is stored."""
    now = datetime.now(UTC)
    seen = min(_instant(seen_through), now)
    with write_transaction(db, scope="plan_pane_view") as conn:
        current = read(conn, user_id=user_id, thread_id=thread_id)
        if current is not None:
            seen = max(seen, _instant(current))
        stored = {"seen_through": _stored(seen)}
        conn.execute(
            """INSERT INTO plan_pane_views (user_id, thread_id, seen_through, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id, thread_id) DO UPDATE SET
                   seen_through=excluded.seen_through,
                   updated_at=excluded.updated_at""",
            (user_id, thread_id, stored["seen_through"], _stored(now)),
        )
    return stored
