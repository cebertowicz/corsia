"""Add task-start trial delivery provenance to guidance receipts."""
from __future__ import annotations

import sqlite3


def migrate_161_to_162(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(guidance_loads)")}
    for name, ddl in (
        ("node_id", "TEXT"),
        ("variant", "TEXT CHECK (variant IS NULL OR variant IN ('with', 'without', 'crossover'))"),
        ("checkpoint_generation_id", "TEXT"),
        ("lesson_id", "TEXT"),
        ("lesson_behavioral_sha256", "TEXT"),
        ("omission_reason", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE guidance_loads ADD COLUMN {name} {ddl}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_guidance_loads_lesson_node "
        "ON guidance_loads(lesson_id, lesson_behavioral_sha256, node_id) WHERE lesson_id IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_guidance_loads_trial_thread_time "
        "ON guidance_loads(thread_id, created_at, lesson_id, lesson_behavioral_sha256, id) "
        "WHERE variant='with' AND lesson_id IS NOT NULL AND lesson_behavioral_sha256 IS NOT NULL"
    )
