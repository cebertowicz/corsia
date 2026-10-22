"""Schema 134 -> 135: whether a suggestion's decision settled several suggestions at once."""

from __future__ import annotations

import sqlite3


def migrate_134_to_135(conn: sqlite3.Connection) -> None:
    """Add the nullable bulk flag; a decision made before it stays NULL, because it was never recorded."""
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='doc_comments'"
    ).fetchone()
    if table is None:
        return
    columns = {row[1] for row in conn.execute("PRAGMA table_info(doc_comments)")}
    if "decided_in_bulk" not in columns:
        conn.execute("ALTER TABLE doc_comments ADD COLUMN decided_in_bulk INTEGER")
