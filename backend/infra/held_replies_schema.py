"""Held replies: a Local only agent's message stored until the person decides.

``infra.held_replies`` reads and writes the rows. ``text`` is the exact bytes that will be
delivered on approval. A row is ``held`` until the person decides; ``approved`` leaves the
person's queue while delivery runs; ``sent``, ``declined`` and ``not_sent`` are final. There is no
expiry column: a held reply waits for the person.
"""

from __future__ import annotations

HELD_REPLIES_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS held_replies (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        sender_thread_id TEXT NOT NULL,
        recipient_thread_id TEXT NOT NULL,
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
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_held_replies_idempotency
        ON held_replies(sender_thread_id, recipient_thread_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_held_replies_user_state
        ON held_replies(user_id, state, created_at);
"""
