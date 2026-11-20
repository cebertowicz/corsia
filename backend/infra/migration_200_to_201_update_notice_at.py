"""Schema 200 -> 201: when an update's check notice was shown to the agent that posted it.

``message_update_state.notice_at`` is set once, when ``core.attention_notice`` appends its check to
the post's result. With ``replaced_at`` and ``seen_at`` it lets Measurement count updates replaced
after the notice and before the person saw them, and read how long after the notice each came.

Updates posted before this column existed have no notice time; nothing recorded one, so none is
invented. Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3


def migrate_200_to_201(conn: sqlite3.Connection) -> None:
    """Add the notice time where the update table lacks it."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(message_update_state)")}
    # A database without the table gets it from the schema fragment, which already has the column.
    if columns and "notice_at" not in columns:
        conn.execute("ALTER TABLE message_update_state ADD COLUMN notice_at TEXT")
