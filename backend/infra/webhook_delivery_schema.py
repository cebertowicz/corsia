"""GitHub delivery log table: one row per admitted delivery, from any source.

``delivery_id`` dedupes a redelivery of the same delivery. ``object_key``
dedupes the same GitHub object arriving from two sources (the webhook and the
notifications poller carry different delivery ids for one comment), so a
comment is admitted once whichever source sees it first.
``check_suite_id`` is the check suite a ``check_suite`` delivery reports on,
so the quiet-PR sweep can ask whether each workflow run's CI event reached this
instance.
``infra.webhook_delivery_log`` is the only writer of both.
"""

from __future__ import annotations

WEBHOOK_DELIVERY_OBJECT_KEY_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_webhook_deliveries_object_key"
    " ON webhook_deliveries(object_key) WHERE object_key IS NOT NULL"
)

WEBHOOK_DELIVERY_CHECK_SUITE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_check_suite"
    " ON webhook_deliveries(check_suite_id, created_at) WHERE check_suite_id IS NOT NULL"
)

WEBHOOK_DELIVERY_SCHEMA_SQL = f"""
    CREATE TABLE IF NOT EXISTS webhook_deliveries (
        id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL,
        action TEXT,
        delivery_id TEXT NOT NULL UNIQUE,
        repo TEXT,
        payload_hash TEXT NOT NULL,
        payload_json TEXT,
        status TEXT NOT NULL DEFAULT 'received',
        error TEXT,
        created_at TEXT NOT NULL,
        object_key TEXT,
        check_suite_id INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_status
        ON webhook_deliveries(status, created_at);
    CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_event
        ON webhook_deliveries(event_type, created_at);
    {WEBHOOK_DELIVERY_OBJECT_KEY_INDEX_SQL};
    {WEBHOOK_DELIVERY_CHECK_SUITE_INDEX_SQL};
"""
