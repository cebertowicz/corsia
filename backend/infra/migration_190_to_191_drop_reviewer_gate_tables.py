"""Schema 190 -> 191: drop what the removed in-source PR reviewer left in an install.

CORSIA no longer reads or writes ``reviewer_gate_state``, ``reviewer_gate_approval`` or
``reviewer_gate_pending``, or the ``reviewer_model`` and ``reviewer_fallback_model`` defaults in the stored
``agents`` settings; an install that still holds them keeps data nothing owns. Dropping is safe to repeat and
to run on a database that never had them or has no settings table yet. An ``agents`` value that is not a JSON object keeps its bytes.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

_HAS_OLD_DEFAULT = (
    "CASE WHEN json_valid(value) THEN "
    "json_type(value,'$.defaults.reviewer_model') IS NOT NULL "
    "OR json_type(value,'$.defaults.reviewer_fallback_model') IS NOT NULL ELSE 0 END"
)


def migrate_190_to_191(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS reviewer_gate_state")
    conn.execute("DROP TABLE IF EXISTS reviewer_gate_approval")
    conn.execute("DROP TABLE IF EXISTS reviewer_gate_pending")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone():
        conn.execute(
            "UPDATE settings SET value=json_remove(value,'$.defaults.reviewer_model',"
            f"'$.defaults.reviewer_fallback_model') WHERE key='agents' AND {_HAS_OLD_DEFAULT}"
        )
