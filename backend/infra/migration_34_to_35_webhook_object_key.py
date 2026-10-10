"""Schema 34 -> 35: object-level dedupe key on ``webhook_deliveries``.

A comment can now arrive from the webhook and from the notifications poller
with different delivery ids, so the log also dedupes on the GitHub object
(``issue_comment:<id>``). Additive: a nullable column and a partial unique
index. Existing rows keep a null key; nothing is backfilled because the
poller's first run starts at its own start time and never overlaps them.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

from infra.webhook_delivery_schema import WEBHOOK_DELIVERY_OBJECT_KEY_INDEX_SQL


def migrate_34_to_35(conn: sqlite3.Connection) -> None:
    """Add ``object_key`` and its partial unique index."""
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(webhook_deliveries)")}
    if "object_key" not in columns:
        conn.execute("ALTER TABLE webhook_deliveries ADD COLUMN object_key TEXT")
    conn.execute(WEBHOOK_DELIVERY_OBJECT_KEY_INDEX_SQL)
