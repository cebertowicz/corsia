"""Schema 102 -> 103: mark turn-ending errors the agent already moved past `recovered`.

Applies the rule `infra.error_recovery` writes at each turn start to the rows
written before it existed. Runs inside the runner's transaction; it never commits.
"""

from __future__ import annotations

import sqlite3

from infra.error_recovery import backfill


def migrate_102_to_103(conn: sqlite3.Connection) -> None:
    # A fixture database that old tests build by hand may carry no message table.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'").fetchone():
        backfill(conn)
