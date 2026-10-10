"""Schema 66 -> 67: store adjacent context for document comment anchors."""

from __future__ import annotations

import sqlite3


def migrate_66_to_67(conn: sqlite3.Connection) -> None:
    """Add nullable TextQuoteSelector-style context columns when needed."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "doc_comments" not in tables:
        return
    columns = {row[1] for row in conn.execute("PRAGMA table_info(doc_comments)")}
    if "anchor_prefix" not in columns:
        conn.execute("ALTER TABLE doc_comments ADD COLUMN anchor_prefix TEXT")
    if "anchor_suffix" not in columns:
        conn.execute("ALTER TABLE doc_comments ADD COLUMN anchor_suffix TEXT")
