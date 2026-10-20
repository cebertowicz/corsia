"""Persist the node-less review-fork linkage without changing Plans."""

from __future__ import annotations

import sqlite3


def migrate_132_to_133(conn: sqlite3.Connection) -> None:
    """Add review-fork and requester-intent references to existing requests."""
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(doc_review_requests)")
    }
    if not columns:
        return
    if "fork_id" not in columns:
        conn.execute("ALTER TABLE doc_review_requests ADD COLUMN fork_id TEXT")
    if "requesting_node_id" not in columns:
        conn.execute(
            "ALTER TABLE doc_review_requests ADD COLUMN requesting_node_id TEXT "
            "REFERENCES plan_nodes(id)"
        )
    if "requirements_revision" not in columns:
        conn.execute(
            "ALTER TABLE doc_review_requests ADD COLUMN requirements_revision INTEGER"
        )
