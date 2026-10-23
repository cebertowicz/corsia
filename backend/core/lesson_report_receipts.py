"""Read task-start lesson offers from durable guidance receipts."""
from __future__ import annotations

import json
from threading import Event
from typing import Any

from core import lesson_report_offers as offers
from core.lesson_report_sql import json_row_chunks


def _rows(db, *, user_id: str, owners: tuple[str, ...], period: bool = False, from_ts: str = "", to_ts: str = ""):
    sql = offers._COLUMNS + offers._BASE
    params: tuple[Any, ...] = (json.dumps(list(owners)), user_id)
    if period:
        sql += " AND g.created_at>=? AND g.created_at<?"
        params += (from_ts, to_ts)
    return json_row_chunks(db, sql + " ORDER BY " + offers.SQL_ORDER, params, offers.FIELDS, offers.ORDER)


def _calls(db, user_id: str, owners: tuple[str, ...], from_ts: str, to_ts: str) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM llm_usage u JOIN threads t ON t.id=u.thread_id "
        "WHERE t.user_id=? AND t.id IN (SELECT value FROM json_each(?)) "
        "AND u.created_at>=? AND u.created_at<?",
        (user_id, json.dumps(list(owners)), from_ts, to_ts),
    ).fetchone()[0]


def read_offers(db, *, user_id: str, thread_ids: tuple[str, ...], from_ts: str, to_ts: str) -> dict[str, Any]:
    owners = tuple(dict.fromkeys(thread_ids))
    if not owners:
        return {"offers": [], "calls": 0, "unavailable_calls": {}}
    rows = list(_rows(db, user_id=user_id, owners=owners, period=True, from_ts=from_ts, to_ts=to_ts))
    return {"offers": rows, "calls": _calls(db, user_id, owners, from_ts, to_ts), "unavailable_calls": {}}


def read_offer_evidence(db, *, user_id: str, thread_ids: tuple[str, ...], from_ts: str, to_ts: str,
                        cancellation_event: Event | None = None) -> dict[str, Any]:
    owners = tuple(dict.fromkeys(thread_ids))
    if not owners:
        empty = offers.DeferredOffers([], user_id, from_ts, to_ts)
        empty.collect(db, cancellation_event=cancellation_event)
        return {"retained": [], "period": {"offers": empty, "calls": 0, "unavailable_calls": {}}}
    retained: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in _rows(db, user_id=user_id, owners=owners):
        retained.setdefault((row["id"], row["version"], row["path"], row["raw_sha256"]), row)
    period = offers.DeferredOffers(list(owners), user_id, from_ts, to_ts)
    period.collect(db, cancellation_event=cancellation_event)
    return {"retained": list(retained.values()), "period": {"offers": period,
            "calls": _calls(db, user_id, owners, from_ts, to_ts), "unavailable_calls": {}}}


def offer_counts(period_offers: offers.DeferredOffers, identity: tuple[str, str]) -> dict[str, Any]:
    counts = period_offers.counts.get(identity, {})
    return {"receipts": counts.get("receipts", 0), "tasks": counts.get("tasks", 0),
            "opportunity": None, "application": None, "efficacy": None,
            "reason": "task-start receipts prove delivery, not reading, use, or benefit"}
