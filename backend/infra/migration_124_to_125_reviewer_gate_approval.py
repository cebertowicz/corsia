"""Schema 124 -> 125: the head a maintainer approved for review.

Purely additive: one new table, no data to convert. The DDL is copied rather
than imported so this step keeps producing the shape it was written for.
"""

from __future__ import annotations

import sqlite3

_REVIEWER_GATE_APPROVAL_124_DDL = """
    CREATE TABLE IF NOT EXISTS reviewer_gate_approval (
        pr_key       TEXT PRIMARY KEY,
        approved_sha TEXT NOT NULL
    )
"""


def migrate_124_to_125(conn: sqlite3.Connection) -> None:
    """Create the reviewer gate approval table."""
    conn.execute(_REVIEWER_GATE_APPROVAL_124_DDL)
