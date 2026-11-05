"""Schema 160 -> 161: persist reviewer runs interrupted by a backend restart."""

from __future__ import annotations

import sqlite3


def migrate_160_to_161(conn: sqlite3.Connection) -> None:
    """Create the exact-head queue used to recover pending reviewer runs."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS reviewer_gate_pending ("
        "pr_key TEXT NOT NULL, head_sha TEXT NOT NULL, "
        "internal_event TEXT NOT NULL, PRIMARY KEY (pr_key, head_sha))"
    )
