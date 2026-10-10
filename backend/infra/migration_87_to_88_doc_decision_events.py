"""Schema 87 -> 88: durable decision-event identities on document comments."""

from __future__ import annotations

import sqlite3


def migrate_87_to_88(conn: sqlite3.Connection) -> None:
    """Add the nullable identity shared by every row in one decision call."""
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='doc_comments'"
    ).fetchone()
    if table is None:
        return
    columns = {row[1] for row in conn.execute("PRAGMA table_info(doc_comments)")}
    if "decision_event_id" not in columns:
        conn.execute("ALTER TABLE doc_comments ADD COLUMN decision_event_id TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_doc_comments_decision_event "
        "ON doc_comments(decision_event_id) WHERE decision_event_id IS NOT NULL"
    )
