"""Schema 63 -> 64: server-side seen state on the presence row.

Purely additive: two nullable ``thread_presence`` columns, no second seen
table and no backfill. ``marked_read_at`` is the Mark read boundary;
``marked_unread_at`` keeps the update dot on until the next opened visit.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

_COLUMNS_64 = ("marked_read_at", "marked_unread_at")


def migrate_63_to_64(conn: sqlite3.Connection) -> None:
    """Add the Mark read/unread columns when they are missing."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(thread_presence)")}
    for column in _COLUMNS_64:
        if column not in existing:
            conn.execute(f"ALTER TABLE thread_presence ADD COLUMN {column} TEXT")
