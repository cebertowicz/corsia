"""Scope review-fork feedback batches to the request that produced them."""

from __future__ import annotations

import sqlite3


def migrate_133_to_134(conn: sqlite3.Connection) -> None:
    """Add a nullable review-request reference and split the open-batch index."""
    columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(doc_feedback_batches)")
    }
    if not columns:
        return
    if "review_request_id" not in columns:
        conn.execute(
            "ALTER TABLE doc_feedback_batches ADD COLUMN review_request_id TEXT "
            "REFERENCES doc_review_requests(id)"
        )
    conn.execute("DROP INDEX IF EXISTS idx_doc_feedback_batches_open")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_doc_feedback_batches_open "
        "ON doc_feedback_batches(author_kind, author_id, artifact_id, recipient_id, review_request_id) "
        "WHERE state='open' AND review_request_id IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_doc_feedback_batches_open_unscoped "
        "ON doc_feedback_batches(author_kind, author_id, artifact_id, recipient_id) "
        "WHERE state='open' AND review_request_id IS NULL"
    )
