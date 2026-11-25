"""Schema 208 -> 209: saved-login fill history, retained after login deletion.

The DDL is copied so this migration always creates its original shape. The
migration runner owns the transaction.
"""

import sqlite3


def migrate_208_to_209(conn: sqlite3.Connection) -> None:
    """Add the fill audit table and its login/time index."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS saved_login_fill_audit (
            id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            login_id TEXT NOT NULL,
            site TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            result TEXT NOT NULL CHECK (result IN ('filled', 'rejected', 'unknown'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_saved_login_fill_audit_login_created
        ON saved_login_fill_audit(login_id, created_at)
    """)
