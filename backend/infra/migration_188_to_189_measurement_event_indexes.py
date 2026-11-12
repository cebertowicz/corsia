"""Schema 188 -> 189: index the plan events Measurement settles outcomes from.

Purely additive: indexes only, no row or column changes. Each is partial, so building one reads the events
table once and stores only the rows its read wants.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

from infra.measurement_schema import PLAN_EVENT_MEASUREMENT_INDEXES


def migrate_188_to_189(conn: sqlite3.Connection) -> None:
    """Create each event index a fresh schema has; a database without the table has nothing to index."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='plan_node_events'").fetchone():
        for sql in PLAN_EVENT_MEASUREMENT_INDEXES:
            conn.execute(sql)
