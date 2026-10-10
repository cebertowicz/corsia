"""Schema 72 -> 73: add durable document-edit feedback evidence."""

from __future__ import annotations

import sqlite3


_EDIT_ITEMS_SQL = """
    CREATE TABLE IF NOT EXISTS doc_feedback_edit_items (
        batch_id TEXT PRIMARY KEY
            REFERENCES doc_feedback_batches(id) ON DELETE CASCADE,
        actor_display TEXT NOT NULL,
        prior_segments_diff TEXT NOT NULL DEFAULT '',
        baseline_sha256 TEXT NOT NULL,
        baseline_text TEXT NOT NULL,
        latest_sha256 TEXT NOT NULL,
        latest_text TEXT NOT NULL,
        save_count INTEGER NOT NULL CHECK (save_count >= 1),
        first_at TEXT NOT NULL,
        last_at TEXT NOT NULL
    );
"""


def migrate_72_to_73(conn: sqlite3.Connection) -> None:
    """Add edit items and the explicit successful-delivery outcome."""
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "doc_feedback_batches" not in tables:
        return
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(doc_feedback_batches)")
    }
    if "delivery_outcome" not in columns:
        conn.execute(
            "ALTER TABLE doc_feedback_batches ADD COLUMN delivery_outcome TEXT "
            "CHECK (delivery_outcome IS NULL OR delivery_outcome IN ('node','ui','empty'))"
        )
    conn.execute(_EDIT_ITEMS_SQL)
