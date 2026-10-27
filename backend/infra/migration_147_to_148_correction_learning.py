"""Schema 148: one triage fact and one terminal outcome per correction subject."""
from __future__ import annotations

import sqlite3

# Frozen at schema v148, like the live definitions in ``outcome_events_schema``.
_V148_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_learning_loop_triage_subject ON outcome_events(subject_key) "
    "WHERE event_name='learning_loop.correction_triage'",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_learning_loop_outcome_subject ON outcome_events(subject_key) "
    "WHERE event_name='learning_loop.correction_outcome'",
)


def migrate_147_to_148(conn: sqlite3.Connection) -> None:
    """Add the two indexes inside the caller's migration transaction.

    A database without ``outcome_events`` has nothing to index; the table is created with them.
    """
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outcome_events'").fetchone() is None:
        return
    for statement in _V148_INDEXES:
        conn.execute(statement)
