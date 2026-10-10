"""The one ``turn_activity`` frame every harness sends when a Turn starts."""

from __future__ import annotations

import sqlite3

from loguru import logger

from core.thread_identity import visible_root_or_none
from infra.database import get_db
from infra.db_async import run_in_db
from infra.thread_inputs import TurnIdentity


def _started_at_or_none(turn_id: str, db: sqlite3.Connection) -> str | None:
    """A display read: a lookup failure degrades to None, never fails admission."""
    try:
        # fetchall steps the statement to completion so the pooled connection
        # is not left holding an open read snapshot.
        rows = db.execute(
            "SELECT started_at FROM active_turns WHERE id=?", (turn_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("turn start lookup failed for {}: {}", turn_id, exc)
        return None
    return str(rows[0]["started_at"]) if rows else None


def _turn_activity_fields(thread_id: str, turn_id: str) -> dict[str, str | None]:
    connection = get_db()
    try:
        return {
            "visible_root_thread_id": visible_root_or_none(thread_id, db=connection),
            "started_at": _started_at_or_none(turn_id, connection),
        }
    finally:
        connection.close()


async def turn_activity_frame(thread_id: str, identity: TurnIdentity) -> dict:
    """Announce one admitted Turn with its visible root and durable start.

    It carries the same lineage and start time as
    ``core.turn_core.active_turns_for_owner``, so a client that learns of the
    Turn live and one that reloads mid-Turn read the same facts.
    """
    fields = await run_in_db(_turn_activity_fields, thread_id, identity.turn_id)
    return {
        "type": "turn_activity",
        "thread_id": thread_id,
        **fields,
        **identity.frame_fields(),
    }
