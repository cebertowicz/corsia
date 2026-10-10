"""Schema 103 -> 104: when a continuity entry was last reviewed.

Adds the nullable ``reviewed_at`` column to ``continuity_entries``. The
checkpoint review orders its rotating queue by it, so a verdict, a keep
included, sends the entry to the back. Existing rows stay null and order by
their own update time, which is when they last changed.

Runs inside the runner's transaction and never commits.
"""

from __future__ import annotations

import sqlite3


def migrate_103_to_104(conn: sqlite3.Connection) -> None:
    if any(row[1] == "reviewed_at" for row in conn.execute("PRAGMA table_info(continuity_entries)")):
        return
    conn.execute("ALTER TABLE continuity_entries ADD COLUMN reviewed_at TEXT")
