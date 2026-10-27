"""Schema 144 -> 145: a connected account's non-secret server settings, for mail hosts."""

from __future__ import annotations

import sqlite3


def migrate_144_to_145(conn: sqlite3.Connection) -> None:
    """Add the nullable ``endpoints_json`` column; Google and Microsoft rows keep NULL."""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(connected_accounts)")}
    if columns and "endpoints_json" not in columns:
        conn.execute(
            "ALTER TABLE connected_accounts ADD COLUMN endpoints_json TEXT "
            "CHECK(endpoints_json IS NULL OR json_valid(endpoints_json))"
        )
