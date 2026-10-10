"""Schema 99 -> 100: add the hourly ``llm_usage`` rollup tables and the created_at index.

Purely additive: three empty tables and two indexes, no row or column changes.
The created_at index reads ``llm_usage`` once (about 0.3s for half a million
rows). The rollup itself is built lazily by the first Measurement read, never
here.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

from infra.usage_rollup_schema import USAGE_ROLLUP_TABLES


def migrate_99_to_100(conn: sqlite3.Connection) -> None:
    """Create the rollup storage a fresh schema has; a database without a ledger gets no ledger index."""
    has_ledger = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_usage'").fetchone()
    for name, sql in USAGE_ROLLUP_TABLES.items():
        if has_ledger or name != "idx_llm_usage_created_at":
            conn.execute(sql)
