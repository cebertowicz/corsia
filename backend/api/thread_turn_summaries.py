"""Whole-Turn summary facts for an already-projected message page."""

from __future__ import annotations

import json

from core.stop_causes import PLAN_LIMIT_REASON
from infra.message_activity import ACTIVITY_ROW_SQL, ACTIVITY_SQL
from infra.time import utc_storage_iso_to_api


def page_turn_ids(display: list[dict]) -> list[str]:
    """Return the durable Turns represented by one display page.

    An input names the Turn that took it, so a Turn stopped before its first
    output is still on the page.
    """
    owned = {
        str(metadata["turn_id"])
        for row in display
        for metadata in [row.get("metadata")]
        if isinstance(metadata, dict)
        and isinstance(metadata.get("turn_id"), str)
        and metadata["turn_id"]
    }
    taken = {row["active_turn_id"] for row in display if isinstance(row.get("active_turn_id"), str) and row["active_turn_id"]}
    return sorted(owned | taken)


def page_turn_summaries(db, thread_id: str, display: list[dict]) -> dict[str, dict]:
    """Read whole-Turn summaries for a page inside its caller's snapshot."""
    turn_ids = page_turn_ids(display)
    if not turn_ids:
        return {}
    params = [thread_id, json.dumps(turn_ids)]
    activity_rows = db.execute(
        "SELECT json_extract(metadata, '$.turn_id') AS turn_id, "
        f"SUM({ACTIVITY_SQL}) AS activity_count "
        "FROM messages INDEXED BY idx_messages_turn_calls "
        f"WHERE thread_id=? AND {ACTIVITY_ROW_SQL} "
        "AND json_extract(metadata, '$.turn_id') IN (SELECT value FROM json_each(?)) "
        "GROUP BY json_extract(metadata, '$.turn_id')",
        params,
    ).fetchall()
    activity_by_turn = {str(row["turn_id"]): int(row["activity_count"]) for row in activity_rows}
    # Keep the page IDs outermost so SQLite seeks each turn by primary key.
    turns = db.execute(
        "SELECT t.id,t.status,t.started_at,t.ended_at,t.error "
        "FROM json_each(?) j CROSS JOIN active_turns t "
        "WHERE t.id=j.value AND t.thread_id=?",
        [params[1], thread_id],
    ).fetchall()
    return {str(row["id"]): _summary(row, activity_by_turn.get(str(row["id"]), 0)) for row in turns}


def _summary(row, activity_count: int) -> dict:
    summary = {
        "started_at": utc_storage_iso_to_api(row["started_at"]),
        "ended_at": utc_storage_iso_to_api(row["ended_at"]),
        "status": row["status"],
        "activity_count": activity_count,
    }
    # Only a plan-limit Stop names its cause; a user Stop's summary is unchanged.
    if row["status"] == "cancelled" and row["error"] == PLAN_LIMIT_REASON:
        summary["cause"] = PLAN_LIMIT_REASON
    return summary
