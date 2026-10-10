"""Schema 69 -> 70: remove the dead reply-window projection index."""

from __future__ import annotations

import sqlite3


def migrate_69_to_70(conn: sqlite3.Connection) -> None:
    """Drop the index that only the removed reply-window reader used."""
    conn.execute("DROP INDEX IF EXISTS idx_messages_unbound_human_turn")
