"""Dead-letter storage for unroutable events."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from infra.database import get_db, write_transaction


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def park(
    internal_event: str,
    payload: dict[str, Any],
    reason: str,
    external_id: str | None = None,
    *,
    db=None,
) -> str:
    """Park an unroutable event and return the row id."""
    logger.error(
        "event-routing: UNROUTABLE {} (external_id={!r}, reason={}) — "
        "parked in dead-letter, NOT delivered to any thread",
        internal_event, external_id, reason,
    )
    try:
        row_id = str(uuid.uuid4())[:12]
        with write_transaction(db, scope="dead_letter_park") as connection:
            connection.execute(
                """INSERT INTO dead_letter_events
                   (id, internal_event, external_id, reason, payload_json,
                    status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'parked', ?)""",
                (row_id, internal_event, external_id, reason,
                 json.dumps(payload), _now()),
            )
        return row_id
    except Exception as exc:  # noqa: BLE001 — never let parking raise into routing
        logger.error("event-routing: failed to persist dead-letter row: {}", exc)
        return ""


def list_parked(*, limit: int = 100, db=None) -> list[dict[str, Any]]:
    """Return parked dead-letter rows, newest first."""
    _db = db or get_db()
    try:
        rows = _db.execute(
            "SELECT * FROM dead_letter_events WHERE status='parked' "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if not db:
            _db.close()
