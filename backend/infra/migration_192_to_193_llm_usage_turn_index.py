"""Schema 192 -> 193: index turn-bound usage receipts.

Purely additive; runs inside the migration runner's transaction and never commits.
"""

from __future__ import annotations

import logging
import sqlite3
import time

from infra.usage_schema import LLM_USAGE_TURN_INDEX


def migrate_192_to_193(conn: sqlite3.Connection) -> None:
    """Create the fresh-schema index when the usage table exists."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_usage'").fetchone():
        started = time.perf_counter()
        conn.execute(LLM_USAGE_TURN_INDEX)
        elapsed_ms = (time.perf_counter() - started) * 1000
        rows = conn.execute("SELECT COUNT(*) FROM llm_usage WHERE turn_id IS NOT NULL").fetchone()[0]
        logging.getLogger("infra.database_migrations").info(
            "Built llm_usage turn index: indexed_rows=%d build_ms=%.1f", rows, elapsed_ms
        )
