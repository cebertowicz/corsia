"""Schema 126 -> 127: connected-account history keeps no foreign key to the account.

Databases that reached 126 created connected_account_audit with ON DELETE
CASCADE and connected_account_migration_journal with a plain reference to
connected_accounts. With SQLite foreign keys on, Remove would then delete the
account's audit or fail on its journal row. This step rebuilds either table
that still carries a foreign key, keeping every row.
"""

from __future__ import annotations

import sqlite3

from infra.connected_accounts_schema import AUDIT_TABLE_SQL, JOURNAL_TABLE_SQL

HISTORY_TABLES = (
    ("connected_account_audit", AUDIT_TABLE_SQL),
    ("connected_account_migration_journal", JOURNAL_TABLE_SQL),
)


def migrate_126_to_127(conn: sqlite3.Connection) -> None:
    """Rebuild each history table made with a foreign key, keeping every row.

    The runner holds foreign keys off for the whole migration, so the rename is safe.
    """
    for table, create in HISTORY_TABLES:
        if not conn.execute(f"PRAGMA foreign_key_list({table})").fetchall():
            continue
        columns = ",".join(row[1] for row in conn.execute(f"PRAGMA table_info({table})"))
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_with_fk")
        conn.execute(create)
        conn.execute(f"INSERT INTO {table}({columns}) SELECT {columns} FROM {table}_with_fk")
        conn.execute(f"DROP TABLE {table}_with_fk")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_connected_account_audit_account ON connected_account_audit(account_id, id)"
    )
