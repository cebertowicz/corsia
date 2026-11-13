"""Schema 190 -> 191: drop the receipt tables of the removed in-source PR reviewer.

CORSIA no longer reads or writes ``reviewer_gate_state``, ``reviewer_gate_approval`` or
``reviewer_gate_pending``; an install that still holds them keeps rows nothing owns. Dropping is safe to
repeat and to run on a database that never had them.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

def migrate_190_to_191(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS reviewer_gate_state")
    conn.execute("DROP TABLE IF EXISTS reviewer_gate_approval")
    conn.execute("DROP TABLE IF EXISTS reviewer_gate_pending")
