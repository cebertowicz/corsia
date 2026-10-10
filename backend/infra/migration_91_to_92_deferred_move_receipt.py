"""Persist an account move receipt until its cross-vendor switch commits."""
from __future__ import annotations


def migrate_91_to_92(conn) -> None:
    """Add the nullable JSON receipt payload to destination account bindings."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(thread_vendor_accounts)")}
    if "deferred_move" not in columns:
        conn.execute("ALTER TABLE thread_vendor_accounts "
                     "ADD COLUMN deferred_move TEXT CHECK (deferred_move IS NULL OR json_valid(deferred_move))")
