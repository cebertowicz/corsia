"""Schema 123 -> 124: drop the unused ``threads.title_source`` column.

No code reads or writes it. Whether a thread still awaits a generated title is
decided by its title text alone (``DEFAULT_THREAD_TITLE`` in ``ws.post_turn``).

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3


def migrate_123_to_124(conn: sqlite3.Connection) -> None:
    """Drop ``threads.title_source`` when it is still present."""
    present = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
    if "title_source" in present:
        conn.execute("ALTER TABLE threads DROP COLUMN title_source")
