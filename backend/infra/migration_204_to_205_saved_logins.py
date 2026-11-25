"""Schema 204 -> 205: saved website logins.

Purely additive: one new table, no data to convert. A login's password is a
key store value and never a column here. The DDL is copied rather than
imported so this step keeps producing the shape it was written for.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

_SAVED_LOGINS_205_DDL = """
    CREATE TABLE IF NOT EXISTS saved_logins (
        id TEXT PRIMARY KEY,
        site TEXT NOT NULL,
        username TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'out_of_date')),
        saved_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_used_at TEXT,
        last_used_by_thread_id TEXT,
        UNIQUE (site, username)
    )
"""


def migrate_204_to_205(conn: sqlite3.Connection) -> None:
    """Create the saved logins table where it is missing."""
    conn.execute(_SAVED_LOGINS_205_DDL)
