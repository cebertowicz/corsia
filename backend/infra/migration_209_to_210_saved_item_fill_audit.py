"""Schema 209 -> 210: the fill history records a saved item's fills too.

A saved item (a Settings › Keys entry, ``infra.saved_items``) is filled into a
page through the same approval and record as a saved login. Its row names the
item's key store entry in the new ``item`` column and leaves ``login_id``
empty; a login's row leaves ``item`` NULL, as every earlier row does.

Purely additive: one nullable column, added rather than rebuilding the table so
an upgrade never rewrites it. Same definition as the fresh-install DDL. Adding
it twice changes nothing. Runs inside the migration runner's transaction; it
never commits.
"""

import sqlite3

ITEM_COLUMN_DDL = "ALTER TABLE saved_login_fill_audit ADD COLUMN item TEXT"


def migrate_209_to_210(conn: sqlite3.Connection) -> None:
    """Add the fill history's item column."""
    columns = [row[1] for row in conn.execute("PRAGMA table_info(saved_login_fill_audit)")]
    if columns and "item" not in columns:
        conn.execute(ITEM_COLUMN_DDL)
