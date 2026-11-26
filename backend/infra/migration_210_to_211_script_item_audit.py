"""Schema 210 -> 211: the fill history records which script used a saved item or login.

A script the person approved to use saved items (``automation.script_items``)
records each use in the same history as a fill. Its row names the script
schedule in the new ``script_id`` column; a fill's row leaves it NULL, as every
earlier row does.

Purely additive: one nullable column, added rather than rebuilding the table so
an upgrade never rewrites it. Same definition as the fresh-install DDL. Adding
it twice changes nothing. Runs inside the migration runner's transaction; it
never commits.
"""

import sqlite3

SCRIPT_COLUMN_DDL = "ALTER TABLE saved_login_fill_audit ADD COLUMN script_id TEXT"


def migrate_210_to_211(conn: sqlite3.Connection) -> None:
    """Add the fill history's script column."""
    columns = [row[1] for row in conn.execute("PRAGMA table_info(saved_login_fill_audit)")]
    if columns and "script_id" not in columns:
        conn.execute(SCRIPT_COLUMN_DDL)
