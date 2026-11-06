"""Schema 183 -> 184: each person and agent acknowledges a comment thread separately.

``doc_comment_acks`` holds one row per thread and acknowledger, and replaces the shared
``doc_comments.acked`` flag. The flag never recorded who set it, so its rows are not carried:
crediting them to anyone would invent an acknowledgement.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3


def migrate_183_to_184(conn: sqlite3.Connection) -> None:
    """Add the per-thread acknowledgements and drop the shared flag."""
    columns = {column[1] for column in conn.execute("PRAGMA table_info(doc_comments)")}
    if not columns:
        # A database whose comments table has not arrived yet: it is created on demand from
        # the schema fragment (infra/doc_comments_schema.py), which already has this shape.
        return
    conn.execute("""CREATE TABLE IF NOT EXISTS doc_comment_acks (
        root_id TEXT NOT NULL REFERENCES doc_comments(id) ON DELETE CASCADE,
        actor_id TEXT NOT NULL,
        actor_name TEXT NOT NULL,
        acked_at TEXT NOT NULL,
        PRIMARY KEY (root_id, actor_id)
    )""")
    if "acked" in columns:
        conn.execute("ALTER TABLE doc_comments DROP COLUMN acked")
