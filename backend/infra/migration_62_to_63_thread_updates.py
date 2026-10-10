"""Schema 62 -> 63: the thread update index.

Purely additive: one new table and its indexes, no data to convert. The DDL
is copied rather than imported from ``infra.thread_updates_schema`` so this
step keeps producing the shape it was written for even after that module's
DDL changes later.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

_THREAD_UPDATES_63_DDL = (
    """
    CREATE TABLE IF NOT EXISTS thread_updates (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        thread_id   TEXT NOT NULL REFERENCES threads(id),
        kind        TEXT NOT NULL CHECK (kind IN (
                      'marked',
                      'done', 'done_follow_up', 'failed', 'closed', 'blocked')),
        source_kind TEXT NOT NULL CHECK (source_kind IN ('message', 'plan_event')),
        source_id   TEXT NOT NULL,
        -- Navigation only: no foreign key, so the card outlives its node.
        node_id     TEXT,
        title       TEXT,
        after_seq   INTEGER NOT NULL CHECK (after_seq >= 0),
        created_at  TEXT NOT NULL,
        UNIQUE (source_kind, source_id),
        CHECK ((kind = 'marked' AND source_kind = 'message') OR
               (kind != 'marked' AND source_kind = 'plan_event')),
        CHECK ((kind = 'marked' AND title IS NULL) OR
               (kind != 'marked' AND title IS NOT NULL AND length(trim(title)) > 0)),
        CHECK (kind = 'marked' OR node_id IS NOT NULL)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_thread_updates_thread_time"
    " ON thread_updates(thread_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_thread_updates_thread_seq"
    " ON thread_updates(thread_id, after_seq)",
    "CREATE INDEX IF NOT EXISTS idx_thread_updates_node"
    " ON thread_updates(node_id, id) WHERE node_id IS NOT NULL",
)


def migrate_62_to_63(conn: sqlite3.Connection) -> None:
    """Create the thread update index on an existing database."""
    for statement in _THREAD_UPDATES_63_DDL:
        conn.execute(statement)
