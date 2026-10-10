"""Schema 54 -> 55: retire duplicate checkpoint continuity FTS indexing."""

from __future__ import annotations

import sqlite3

from infra.db_fts import _init_checkpoints_fts

_TRIGGERS = (
    "checkpoints_fts_insert",
    "checkpoints_fts_delete",
    "checkpoints_fts_update",
)


def migrate_54_to_55(conn: sqlite3.Connection) -> None:
    """Rebuild checkpoint FTS from narratives, retaining all source rows."""
    for trigger in _TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    conn.execute("DROP TABLE IF EXISTS checkpoints_fts")
    _init_checkpoints_fts(conn)
    conn.execute("INSERT INTO checkpoints_fts(checkpoints_fts) VALUES('rebuild')")
