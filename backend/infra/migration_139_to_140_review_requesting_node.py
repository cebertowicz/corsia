"""Schema 139 -> 140: a review request names the task its requester was on."""
from __future__ import annotations

import sqlite3


def migrate_139_to_140(conn: sqlite3.Connection) -> None:
    """Add the nullable reference; existing requests keep reading as unreferenced."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "doc_review_requests" not in tables:
        return
    columns = {row[1] for row in conn.execute("PRAGMA table_info(doc_review_requests)")}
    if "requesting_node_id" not in columns:
        conn.execute("ALTER TABLE doc_review_requests ADD COLUMN requesting_node_id TEXT REFERENCES plan_nodes(id)")
