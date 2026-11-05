"""Schema 174 -> 175: reviewer runs a restart may have cut off.

Purely additive: one new table, no data to convert. The DDL is copied rather
than imported so this step keeps producing the shape it was written for.
"""

from __future__ import annotations

import sqlite3

_REVIEWER_GATE_PENDING_174_DDL = """
    CREATE TABLE IF NOT EXISTS reviewer_gate_pending (
        pr_key         TEXT NOT NULL,
        head_sha       TEXT NOT NULL,
        internal_event TEXT NOT NULL,
        PRIMARY KEY (pr_key, head_sha)
    )
"""


def migrate_174_to_175(conn: sqlite3.Connection) -> None:
    """Create the reviewer gate pending-run table."""
    conn.execute(_REVIEWER_GATE_PENDING_174_DDL)
