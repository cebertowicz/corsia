"""Schema 127 -> 128: the account audit's `manual_only` revocation cause is `not_signed_in`.

`manual_only` meant "no refresh token was held, so nothing was revoked". It read
as an instruction to the person, but it only ever happened on a row that was
already signed out, whose revoke ran at Sign out. The cause is renamed, and the
audit table's check constraint with it, so this step rebuilds the table and
renames the cause on every row that has it.
"""

from __future__ import annotations

import sqlite3

from infra.connected_accounts_schema import AUDIT_TABLE_SQL

TABLE = "connected_account_audit"


def migrate_127_to_128(conn: sqlite3.Connection) -> None:
    """Rebuild the audit table whose check still lists `manual_only`, keeping every row.

    The runner holds foreign keys off for the whole migration, so the rename is safe.
    """
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (TABLE,)).fetchone()
    if sql is None or "not_signed_in" in sql[0]:
        return
    columns = [row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})")]
    copied = ",".join(
        "CASE WHEN revocation_cause='manual_only' THEN 'not_signed_in' ELSE revocation_cause END"
        if column == "revocation_cause" else column
        for column in columns
    )
    conn.execute(f"ALTER TABLE {TABLE} RENAME TO {TABLE}_old_cause")
    conn.execute(AUDIT_TABLE_SQL)
    conn.execute(f"INSERT INTO {TABLE}({','.join(columns)}) SELECT {copied} FROM {TABLE}_old_cause")
    conn.execute(f"DROP TABLE {TABLE}_old_cause")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_connected_account_audit_account ON {TABLE}(account_id, id)")
