"""Schema 126 -> 127: stamp errors that a seed alone held open `recovered`.

A seed is never drawn, so it no longer holds a turn-ending error open (the
rule `infra.error_recovery` writes at each turn start). Rows backfilled while a
seed still counted are stamped here by rerunning that backfill: it only adds
stamps, and an error already stamped is left as it is. Runs inside the
runner's transaction; it never commits.
"""

from __future__ import annotations

import sqlite3

from infra.error_recovery import backfill


def migrate_126_to_127(conn: sqlite3.Connection) -> None:
    """Stamp every open error the agent moved past under the current rule."""
    # A fixture database that old tests build by hand may carry no message table.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'").fetchone():
        backfill(conn)
