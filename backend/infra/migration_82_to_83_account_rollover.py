"""Add an explicit per-project/vendor account-limit switch, disabled by default."""
from __future__ import annotations

import sqlite3


def migrate_82_to_83(conn: sqlite3.Connection) -> None:
    """Preserve account policy/order and install opt-in in the migration transaction."""
    conn.execute("ALTER TABLE project_vendor_accounts ADD COLUMN switch_on_limit "
                 "INTEGER NOT NULL DEFAULT 0 CHECK (switch_on_limit IN (0,1))")
