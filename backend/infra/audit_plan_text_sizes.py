"""How long the text agents write into plans is, for ``plan_activity``.

Only sizes leave this module: each text is measured where it is read and
dropped, so no plan text reaches the caller.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable

from infra.audit_plan_activity import _EVENT_WINDOW, _PLAN_SCOPE

#: Percentiles reported per field, nearest-rank.
_PERCENTILES = (50, 90, 99)

_EVENTS = _PLAN_SCOPE + "CROSS JOIN plan_node_events e INDEXED BY idx_plan_node_events_node ON e.node_id=n.id "
_NODE_WINDOW = (
    "n.created_at>=? AND n.created_at<? "
    "AND julianday(n.created_at)>=julianday(?) AND julianday(n.created_at)<julianday(?)"
)

#: One statement per stored text; a watch_armed row carries two (blocked, set-aside).
_STATEMENTS = {
    "hold_reason": (
        "SELECT h.reason" + _EVENTS
        + "CROSS JOIN plan_node_holds h ON h.id=json_extract(e.payload_json,'$.hold_id') "
        "WHERE t.user_id=? AND e.event='hold_placed' AND json_valid(e.payload_json) AND "
        + _EVENT_WINDOW
    ),
    "wait_reason": (
        "SELECT json_extract(e.payload_json,'$.blocked.reason'), "
        "json_extract(e.payload_json,'$.scheduled.reason')" + _EVENTS
        + "WHERE t.user_id=? AND e.event='watch_armed' AND json_valid(e.payload_json) AND "
        + _EVENT_WINDOW
    ),
    "review_feedback": (
        "SELECT json_extract(e.payload_json,'$.feedback')" + _EVENTS
        + "WHERE t.user_id=? AND e.event='review_rejected' AND json_valid(e.payload_json) AND "
        + _EVENT_WINDOW
    ),
    "review_evidence": (
        "SELECT json_extract(e.payload_json,'$.evidence')" + _EVENTS
        + "WHERE t.user_id=? AND e.event='review_evidence_submitted' "
        "AND json_valid(e.payload_json) AND " + _EVENT_WINDOW
    ),
    "intent": (
        "SELECT n.intent" + _PLAN_SCOPE + "WHERE t.user_id=? AND " + _NODE_WINDOW
    ),
    "person_step_description": (
        "SELECT n.description" + _PLAN_SCOPE
        + "WHERE t.user_id=? AND n.kind='task' AND n.assignee='user' AND " + _NODE_WINDOW
    ),
}


def _summary(sizes: list[int]) -> dict[str, int | None]:
    """Nearest-rank percentiles and the maximum of one list of sizes."""
    ordered = sorted(sizes)
    count = len(ordered)

    def rank(percent: int) -> int | None:
        return ordered[max(0, -(-percent * count // 100) - 1)] if count else None

    return {f"p{percent}": rank(percent) for percent in _PERCENTILES} | {
        "max": ordered[-1] if count else None,
    }


def _measure(texts: Iterable[object]) -> dict:
    chars: list[int] = []
    emitted: list[int] = []
    for value in texts:
        if isinstance(value, str) and value:
            chars.append(len(value))
            emitted.append(len(json.dumps(value)))
    return {"n": len(chars), "chars": _summary(chars), "json_bytes": _summary(emitted)}


def text_sizes(db: sqlite3.Connection, user_id: str, bounds: tuple[str, ...]) -> dict[str, dict]:
    """Return each plan text field's size distribution inside the window."""
    sizes = {}
    for field, statement in _STATEMENTS.items():
        rows = db.execute(statement, (user_id, *bounds))
        if field == "wait_reason":
            blocked: list[object] = []
            set_aside: list[object] = []
            for blocked_reason, set_aside_reason in rows:
                blocked.append(blocked_reason)
                set_aside.append(set_aside_reason)
            sizes["blocked_wait_reason"] = _measure(blocked)
            sizes["set_aside_reason"] = _measure(set_aside)
        else:
            sizes[field] = _measure(row[0] for row in rows)
    return sizes

