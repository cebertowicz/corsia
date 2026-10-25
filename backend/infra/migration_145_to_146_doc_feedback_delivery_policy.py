"""Schema 145 -> 146: separate durable document-feedback delivery lanes."""
from __future__ import annotations

import sqlite3


def migrate_145_to_146(conn: sqlite3.Connection) -> None:
    """Give existing batches the compatible immediate lane and make lanes non-coalescing."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "doc_feedback_batches" not in tables:
        return
    columns = {row[1] for row in conn.execute("PRAGMA table_info(doc_feedback_batches)")}
    if "delivery_policy" not in columns:
        conn.execute(
            "ALTER TABLE doc_feedback_batches ADD COLUMN delivery_policy TEXT NOT NULL DEFAULT 'immediate' "
            "CHECK (delivery_policy IN ('immediate','document-held','image-held'))"
        )
    conn.execute("DROP INDEX IF EXISTS idx_doc_feedback_batches_open")
    conn.execute(
        "CREATE UNIQUE INDEX idx_doc_feedback_batches_open\n"
        "        ON doc_feedback_batches(author_kind, author_id, artifact_id, recipient_id, delivery_policy)\n"
        "        WHERE state='open'"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_doc_feedback_batches_recipient_open "
        "ON doc_feedback_batches(recipient_id, author_kind, author_id, delivery_policy, state)"
    )
