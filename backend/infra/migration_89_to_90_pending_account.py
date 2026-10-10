"""Hold an account a person chose while a turn was running until the next turn claims."""
from __future__ import annotations


def migrate_89_to_90(conn) -> None:
    # An older database may have created thread_vendor_accounts from the current
    # DDL in an earlier step, so the column can already exist.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(thread_vendor_accounts)")}
    if "pending_account_id" not in columns:
        conn.execute("ALTER TABLE thread_vendor_accounts "
                     "ADD COLUMN pending_account_id TEXT REFERENCES vendor_accounts(id)")
