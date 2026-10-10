"""Schema 61 -> 62: remove the obsolete unreadable-metadata index.

All production writers serialize unbound human-message metadata as JSON, so
reply-window projection no longer needs a thread-wide corrupt-row guard. The
remaining metadata-Turn index still serves the only live unbound-human read.
"""

from __future__ import annotations

import sqlite3


def migrate_61_to_62(conn: sqlite3.Connection) -> None:
    """Drop the index that existed only for removed unreadable-metadata detection."""
    conn.execute("DROP INDEX IF EXISTS idx_messages_unbound_human_unreadable")
