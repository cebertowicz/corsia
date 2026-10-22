"""Schema 133 -> 134: when a pending suggestion's author was told its text was changed."""

from __future__ import annotations

import sqlite3


def migrate_133_to_134(conn: sqlite3.Connection) -> None:
    """Add the nullable time an author was told a suggestion could no longer be applied."""
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='doc_comments'"
    ).fetchone()
    if table is None:
        return
    columns = {row[1] for row in conn.execute("PRAGMA table_info(doc_comments)")}
    if "stranded_notified_at" not in columns:
        conn.execute("ALTER TABLE doc_comments ADD COLUMN stranded_notified_at TEXT")
