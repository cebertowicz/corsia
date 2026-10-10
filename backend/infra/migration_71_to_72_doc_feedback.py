"""Schema 71 -> 72: add durable document-feedback batches and views."""

from __future__ import annotations

import sqlite3

from infra.doc_feedback_schema import DOC_FEEDBACK_SCHEMA_SQL


def migrate_71_to_72(conn: sqlite3.Connection) -> None:
    """Create the feedback-batch, item-link, and comment-view tables."""
    for statement in DOC_FEEDBACK_SCHEMA_SQL.split(';'):
        if statement.strip():
            conn.execute(statement)
