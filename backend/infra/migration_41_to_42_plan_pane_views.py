"""Schema 41 -> 42: what each user has seen of a thread's plan in the pane.

One new table, seeded with what the previous build already treated as seen.
That build folded finished work settled before the reader's last exit from the
thread (``thread_presence.last_exited_at``, read by the old
``operator_projection._seen_since`` whenever a presence row exists). Carrying
that exit over as both watermarks keeps every row the reader was shown folded
still folded, and holds everything settled since, so the upgrade neither
replays old work nor marks unseen work seen. A thread the reader never left
gets no row: to the pane, nothing in it has been seen yet.

The DDL is copied rather than imported from ``infra.plan_pane_views_schema`` so
this step keeps producing the shape it was written for even after that
module's DDL changes later.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

_PLAN_PANE_VIEWS_42_DDL = """
    CREATE TABLE IF NOT EXISTS plan_pane_views (
        user_id TEXT NOT NULL,
        thread_id TEXT NOT NULL REFERENCES threads(id),
        seen_through TEXT NOT NULL,
        folded_through TEXT NOT NULL CHECK (folded_through <= seen_through),
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, thread_id)
    )
"""


def _watermark(value: str) -> str:
    # Presence stores naive UTC (an explicit offset is converted); the pane
    # view stores one zoned, fixed-width shape.
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")


def migrate_41_to_42(conn: sqlite3.Connection) -> None:
    """Create the plan pane view table and seed it from each reader's last exit."""
    conn.execute(_PLAN_PANE_VIEWS_42_DDL)
    now = datetime.now(UTC).isoformat(timespec="microseconds")
    rows = conn.execute(
        "SELECT user_id, thread_id, last_exited_at FROM thread_presence"
        " WHERE last_exited_at IS NOT NULL"
    ).fetchall()
    conn.executemany(
        """INSERT OR IGNORE INTO plan_pane_views
               (user_id, thread_id, seen_through, folded_through, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        [
            (user_id, thread_id, _watermark(exited), _watermark(exited), now)
            for user_id, thread_id, exited in rows
        ],
    )
