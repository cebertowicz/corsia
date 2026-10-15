"""Schema 115 -> 116: drafts of outbound actions.

Purely additive: one new table, no data to convert. The DDL is copied rather
than imported so this step keeps producing the shape it was written for.
"""

from __future__ import annotations

import sqlite3

_ACTION_DRAFTS_116_DDL = """
    CREATE TABLE IF NOT EXISTS action_drafts (
        id                TEXT PRIMARY KEY,
        thread_id         TEXT NOT NULL REFERENCES threads(id),
        kind              TEXT NOT NULL CHECK (kind IN ('email', 'invite')),
        path              TEXT NOT NULL,
        target_json       TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(target_json)),
        state             TEXT NOT NULL DEFAULT 'open'
                          CHECK (state IN ('open', 'sending', 'sent', 'discarded')),
        recorded_revision TEXT NOT NULL,
        recorded_at       TEXT NOT NULL,
        created_at        TEXT NOT NULL,
        sent_at           TEXT,
        external_id       TEXT
    )
"""


def migrate_115_to_116(conn: sqlite3.Connection) -> None:
    """Create the action drafts table."""
    conn.execute(_ACTION_DRAFTS_116_DDL)
