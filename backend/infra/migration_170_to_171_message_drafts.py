"""Schema 170 -> 171: action drafts may be texts.

``action_drafts.kind`` gains ``message``. SQLite cannot alter a CHECK, so the table is rebuilt. The
rebuild only widens: every row legal at 170 is legal at 171. The DDL below is a copy of schema 171's
table, not an import of the live constant, so this step keeps producing the shape it was written for.
"""

from __future__ import annotations

import sqlite3

_OLD_TABLE = "action_drafts_v170"

_COLUMNS_170 = (
    "id", "thread_id", "kind", "path", "target_json", "state", "recorded_revision",
    "recorded_at", "created_at", "sent_at", "external_id",
)

_TABLE_171 = """
    CREATE TABLE action_drafts (
        id                TEXT PRIMARY KEY,
        thread_id         TEXT NOT NULL REFERENCES threads(id),
        kind              TEXT NOT NULL CHECK (kind IN ('email', 'invite', 'message')),
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


def migrate_170_to_171(conn: sqlite3.Connection) -> None:
    """Rebuild ``action_drafts`` so its kind CHECK admits ``message``."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='action_drafts'").fetchone() is None:
        return
    columns = ",".join(f'"{name}"' for name in _COLUMNS_170)
    conn.execute(f'ALTER TABLE action_drafts RENAME TO "{_OLD_TABLE}"')
    conn.execute(_TABLE_171)
    conn.execute(f'INSERT INTO action_drafts ({columns}) SELECT {columns} FROM "{_OLD_TABLE}" ORDER BY rowid')
    conn.execute(f'DROP TABLE "{_OLD_TABLE}"')
