"""Schema 173 -> 174: the held-reply table.

A new table only, so no row moves. The DDL below is a copy of schema 174's table, not an import of
the live constant, so this step keeps producing the shape it was written for.
"""

from __future__ import annotations

import sqlite3

_HELD_REPLIES_174 = (
    """
    CREATE TABLE IF NOT EXISTS held_replies (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        sender_thread_id TEXT NOT NULL,
        recipient_thread_id TEXT NOT NULL,
        recipient_title TEXT,
        text TEXT NOT NULL CHECK (state IN ('sent', 'declined', 'not_sent') OR length(trim(text)) > 0),
        via TEXT NOT NULL CHECK (via IN ('tool', 'api')),
        plan_node_id TEXT,
        idempotency_key TEXT,
        state TEXT NOT NULL DEFAULT 'held'
            CHECK (state IN ('held', 'approved', 'sent', 'declined', 'not_sent')),
        error TEXT,
        error_ref TEXT,
        created_at TEXT NOT NULL,
        decided_at TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_held_replies_idempotency
        ON held_replies(sender_thread_id, recipient_thread_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_held_replies_user_state
        ON held_replies(user_id, state, created_at)
    """,
)


def migrate_173_to_174(conn: sqlite3.Connection) -> None:
    """Create ``held_replies`` and its indexes."""
    for statement in _HELD_REPLIES_174:
        conn.execute(statement)
