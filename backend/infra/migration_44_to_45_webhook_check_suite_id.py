"""Schema 44 -> 45: the check suite on ``check_suite`` delivery-log rows.

The quiet-PR sweep now asks the delivery log whether each workflow run's CI
event reached this instance, so the log records the check suite a
``check_suite`` delivery reports on. Additive: a nullable column and a partial
index. Older rows keep a null suite; their payloads were not stored, so
nothing can be backfilled, and the sweep treats CI finished before the first
recorded suite as unknown rather than lost.

The per-PR ``pr_ingress:`` receipt rows in ``settings`` are deleted: the log
replaces them and nothing reads them any more.

The DDL is copied rather than imported so this step keeps producing the shape
it was written for. Runs inside the runner's transaction; it never commits or
rolls back.
"""

from __future__ import annotations

import sqlite3


def migrate_44_to_45(conn: sqlite3.Connection) -> None:
    """Add ``check_suite_id`` and its index; drop the retired ingress receipts."""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(webhook_deliveries)")}
    if "check_suite_id" not in columns:
        conn.execute("ALTER TABLE webhook_deliveries ADD COLUMN check_suite_id INTEGER")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_check_suite"
        " ON webhook_deliveries(check_suite_id, created_at) WHERE check_suite_id IS NOT NULL"
    )
    conn.execute("DELETE FROM settings WHERE substr(key, 1, 11) = 'pr_ingress:'")
