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
        at = datetime.now(timezone.utc).isoformat()
        with write_transaction(None, scope="observe_self_caused") as db:
            db.executemany(
                "INSERT OR REPLACE INTO self_caused_events (connector,event_key,thread_id,at) VALUES (?,?,?,?)",
                [(connector, key, thread, at) for connector, key, thread in keys],
            )


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
