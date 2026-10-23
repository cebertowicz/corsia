"""Schema 144 -> 145: correction learning and trial-delivery receipt provenance."""
from __future__ import annotations

import sqlite3


def migrate_144_to_145(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_learning_loop_triage_subject ON outcome_events(subject_key) "
        "WHERE event_name='learning_loop.correction_triage'"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_learning_loop_outcome_subject ON outcome_events(subject_key) "
        "WHERE event_name='learning_loop.correction_outcome'"
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(guidance_loads)")}
    for name, ddl in (
        ("node_id", "TEXT"),
        ("variant", "TEXT CHECK (variant IS NULL OR variant IN ('with', 'without', 'crossover'))"),
        ("checkpoint_generation_id", "TEXT"),
        ("lesson_id", "TEXT"),
        ("lesson_behavioral_sha256", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE guidance_loads ADD COLUMN {name} {ddl}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_guidance_loads_lesson_node "
        "ON guidance_loads(lesson_id, lesson_behavioral_sha256, node_id) WHERE lesson_id IS NOT NULL"
    )
