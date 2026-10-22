"""Schema 141 -> 142: correction learning and trial-delivery receipt provenance."""
from __future__ import annotations

import sqlite3


def migrate_141_to_142(conn: sqlite3.Connection) -> None:
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
        "CREATE INDEX IF NOT EXISTS idx_guidance_loads_install_path "
        "ON guidance_loads(checkpoint_generation_id, path, lesson_id, lesson_behavioral_sha256, variant) "
        "WHERE checkpoint_generation_id IS NOT NULL"
    )
