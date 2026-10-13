"""Schema 113 -> 114: give CLI harnesses an independent enabled setting."""

from __future__ import annotations

import sqlite3


def migrate_113_to_114(conn: sqlite3.Connection) -> None:
    """Add the switch without changing any observed source status."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(connections)")}
    # Historical partial-schema fixtures predate the connections table; their
    # owning migration chain remains valid without a provider switch to add.
    if columns and "enabled" not in columns:
        conn.execute("ALTER TABLE connections ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
