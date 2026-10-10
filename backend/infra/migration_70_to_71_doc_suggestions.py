"""Schema 70 -> 71: store document suggestion decisions."""

from __future__ import annotations

import sqlite3


_COLUMNS = {
    "replacement": "TEXT",
    "decision": "TEXT",
    "decided_by": "TEXT",
    "decided_by_thread_id": "TEXT",
    "decided_at": "TEXT",
}


def migrate_70_to_71(conn: sqlite3.Connection) -> None:
    """Add nullable suggestion and decision fields to document comments."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "doc_comments" not in tables:
        return
    columns = {row[1] for row in conn.execute("PRAGMA table_info(doc_comments)")}
    for name, kind in _COLUMNS.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE doc_comments ADD COLUMN {name} {kind}")
