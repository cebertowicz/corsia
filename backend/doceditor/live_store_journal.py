"""Journal mode of a document live store."""
from __future__ import annotations

import sqlite3


def enter_wal(connection: sqlite3.Connection) -> None:
    """Move a healthy rollback-journal store to WAL, once; the mode is kept in the file.

    WAL keeps a commit in flight from blocking other connections and reads. Changing the
    mode rewrites header bytes, so a store that fails its integrity check is left exactly
    as found: a refused store's bytes may hold acknowledged typing.
    """
    if connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
        return
    try:
        healthy = connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    except sqlite3.DatabaseError:  # damage is the reads' to report, not this open's
        return
    if healthy:
        connection.execute("PRAGMA journal_mode=WAL")
