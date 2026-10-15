"""Schema 116 -> 117: schedules may be model-free scripts.

``automation_schedules.kind`` gains ``script`` and the row gains the columns a
script run needs: its pinned file, the file's sha256, its timeout, the mark of
a run in flight, and the last run's status and error. SQLite cannot alter a
CHECK, so the table is rebuilt. The rebuild only widens: every row legal at 116
is legal at 117, and the new columns start empty.

The DDL below is a copy of schema 117's table, not an import of the live
constant, so this step keeps producing the shape it was written for.
"""

from __future__ import annotations

import sqlite3

_OLD_TABLE = "automation_schedules_v116"

_COLUMNS_116 = (
    "id", "thread_id", "scope_id", "kind", "spec", "next_run_at", "last_run_at",
    "runs", "max_runs", "state", "payload_json", "created_by_actor",
    "created_at", "updated_at",
)

_TABLE_117 = """
    CREATE TABLE automation_schedules (
        id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL REFERENCES threads(id),
        scope_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('clock', 'cadence', 'script')),
        spec TEXT NOT NULL,
        next_run_at TEXT NOT NULL,
        last_run_at TEXT,
        runs INTEGER NOT NULL DEFAULT 0,
        max_runs INTEGER NOT NULL CHECK (max_runs > 0),
        state TEXT NOT NULL DEFAULT 'active'
            CHECK (state IN ('active', 'paused', 'cancelled', 'exhausted')),
        payload_json TEXT,
        created_by_actor TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        script_path TEXT,
        script_sha256 TEXT,
        timeout_seconds INTEGER,
        running_since TEXT,
        last_status TEXT,
        last_error TEXT,
        noted_at TEXT
    )
"""

_INDEXES = (
    "CREATE INDEX idx_automation_schedules_due"
    " ON automation_schedules(state, next_run_at)",
    "CREATE INDEX idx_automation_schedules_scope"
    " ON automation_schedules(scope_id, next_run_at)",
)


def migrate_116_to_117(conn: sqlite3.Connection) -> None:
    """Rebuild ``automation_schedules`` so its kind CHECK admits ``script``."""
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='automation_schedules'"
    ).fetchone() is None:
        return
    columns = ",".join(f'"{name}"' for name in _COLUMNS_116)
    conn.execute(f'ALTER TABLE automation_schedules RENAME TO "{_OLD_TABLE}"')
    for index in ("idx_automation_schedules_due", "idx_automation_schedules_scope"):
        conn.execute(f'DROP INDEX IF EXISTS "{index}"')
    conn.execute(_TABLE_117)
    conn.execute(
        f'INSERT INTO automation_schedules ({columns}) '
        f'SELECT {columns} FROM "{_OLD_TABLE}" ORDER BY rowid'
    )
    conn.execute(f'DROP TABLE "{_OLD_TABLE}"')
    for statement in _INDEXES:
        conn.execute(statement)
