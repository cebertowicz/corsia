"""Record source event identities created by a connected-app result."""
from __future__ import annotations

from datetime import datetime, timezone

from infra.database import get_db, write_transaction
from tools.mcp_stamp import APP_FIELD, result_stamp


def record_result(thread_id: str | None, result: object) -> None:
    """Record every claimed event before the result is persisted, independently of rules."""
    from events import connectors
    from infra.tool_result_errors import is_error_result

    stamp = result_stamp(result)
    if not thread_id or not stamp or is_error_result(result):
        return
    keys: list[tuple[str, str, str]] = []
    for name in connectors.registered_connectors():
        if not connectors.app_matches(stamp.get(APP_FIELD), name):
            continue
        for extract in connectors.get_manifest(name).artifact_extractors.values():
            artifact = extract(str(result))
            if artifact:
                keys.extend((name, key, thread_id) for key in artifact.created_event_ids)
    if keys:
        from events.rules import RETENTION

        now = datetime.now(timezone.utc)
        at = now.isoformat()
        cutoff = (now - RETENTION).isoformat()
        with write_transaction(None, scope="observe_self_caused") as db:
            db.execute("DELETE FROM self_caused_events WHERE at<?", (cutoff,))
            db.executemany(
                "INSERT OR REPLACE INTO self_caused_events (connector,event_key,thread_id,at) VALUES (?,?,?,?)",
                [(connector, key, thread, at) for connector, key, thread in keys],
            )


def delivery_is_self_caused(connector: str, delivery_id: str | None, thread_id: str, db=None) -> bool:
    """Resolve a delivery's source identity and check the caller's marker."""
    from events.rules import RETENTION

    if not delivery_id:
        return False
    own_connection = db is None
    if own_connection:
        db = get_db()
    try:
        cutoff = (datetime.now(timezone.utc) - RETENTION).isoformat()
        return db.execute(
            "SELECT 1 FROM webhook_deliveries d JOIN self_caused_events s ON s.event_key=d.object_key "
            "WHERE d.delivery_id=? AND s.connector=? AND s.thread_id=? AND s.at>=?",
            (delivery_id, connector, thread_id, cutoff),
        ).fetchone() is not None
    finally:
        if own_connection:
            db.close()


def is_self_caused(connector: str, event_key: str, thread_id: str) -> bool:
    from events.rules import RETENTION

    cutoff = (datetime.now(timezone.utc) - RETENTION).isoformat()
    db = get_db()
    try:
        return db.execute(
            "SELECT 1 FROM self_caused_events WHERE connector=? AND event_key=? AND thread_id=? AND at>=?",
            (connector, event_key, thread_id, cutoff),
        ).fetchone() is not None
    finally:
        db.close()
