"""Schema 145 -> 146: retain the immutable person principal on document comments."""
from __future__ import annotations

import sqlite3


def migrate_145_to_146(conn: sqlite3.Connection) -> None:
    """Add the optional person-author id; legacy and agent rows deliberately remain NULL."""
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='doc_comments'"
    ).fetchone() is None:
        return
    columns = {row[1] for row in conn.execute("PRAGMA table_info(doc_comments)")}
    if "author_user_id" not in columns:
        conn.execute("ALTER TABLE doc_comments ADD COLUMN author_user_id TEXT")
