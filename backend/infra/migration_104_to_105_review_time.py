"""Schema 104 -> 105: when a continuity entry was last reviewed.

Adds the nullable ``reviewed_at`` column to ``continuity_entries``. The
checkpoint review orders its rotating queue by it, so a verdict, a keep
included, sends the entry to the back. Existing rows stay null and order by
their own update time, which is when they last changed.

Runs inside the runner's transaction and never commits.
"""

from __future__ import annotations

import sqlite3


def migrate_104_to_105(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(continuity_entries)")}
    # A fixture database that old tests build by hand may carry no continuity
    # table; the full schema creates it with the column.
    if not columns or "reviewed_at" in columns:
        return
    conn.execute("ALTER TABLE continuity_entries ADD COLUMN reviewed_at TEXT")
