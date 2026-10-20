"""Schema 141 -> 142: one triage and terminal outcome per correction subject."""
from __future__ import annotations

import sqlite3


def migrate_141_to_142(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE UNIQUE INDEX idx_learning_loop_triage_subject ON outcome_events(subject_key) "
        "WHERE event_name='learning_loop.correction_triage'"
    )
    conn.execute(
        "CREATE UNIQUE INDEX idx_learning_loop_outcome_subject ON outcome_events(subject_key) "
        "WHERE event_name='learning_loop.correction_outcome'"
    )
