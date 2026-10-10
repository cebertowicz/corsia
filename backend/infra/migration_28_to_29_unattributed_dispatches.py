"""Schema 28 -> 29: per-Turn unattributed-work-call measurement table.

Purely additive, like schema 26's accounts step: one new table and its index,
no data to convert. The DDL is copied rather than imported from
``infra.unattributed_dispatch_schema`` so this step keeps producing the shape
it was written for, exactly as the accounts step does, even after that
module's DDL changes later.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

_UNATTRIBUTED_DISPATCH_29_DDL = (
    """
    CREATE TABLE IF NOT EXISTS plan_unattributed_dispatches (
        turn_id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL,
        call_count INTEGER NOT NULL CHECK (call_count > 0),
        plan_state TEXT NOT NULL CHECK (plan_state IN ('active', 'paused', 'none')),
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_plan_unattributed_dispatches_thread"
    " ON plan_unattributed_dispatches(thread_id, created_at)",
)


def migrate_28_to_29(conn: sqlite3.Connection) -> None:
    """Create the unattributed-dispatch measurement table on an existing database."""
    for statement in _UNATTRIBUTED_DISPATCH_29_DDL:
        conn.execute(statement)
