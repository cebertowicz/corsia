"""The set_aside audit metric: events set aside instead of delivered, per day, by kind and reason.

Read through ``infra.audit_reads``, which binds the caller's scope and deadline.
An event is set aside when ``events.dead_letter.park`` (or the restart webhook
replay) writes a ``dead_letter_events`` row instead of waking an agent. The
read returns counts only: no payload, external id or thread.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

# Rows are written with more than one ISO form, so the text bounds only narrow
# the index range (a day wide on each side) and julianday() decides. Each
# status is one range on idx_dead_letter_status(status, created_at), stepped to
# by seeks, so the work follows the window, never the table's history.
_SET_ASIDE_SQL = """
WITH RECURSIVE statuses(status) AS (
    SELECT (SELECT status FROM dead_letter_events ORDER BY status LIMIT 1)
    UNION ALL
    SELECT (SELECT n.status FROM dead_letter_events n WHERE n.status>s.status
            ORDER BY n.status LIMIT 1)
    FROM statuses s WHERE s.status IS NOT NULL
)
SELECT d.internal_event, d.reason, date(d.created_at) AS day, count(*) AS n
FROM statuses s CROSS JOIN dead_letter_events d ON d.status=s.status
WHERE s.status IS NOT NULL
  AND d.created_at>=:since_lo AND d.created_at<:until_hi
  AND julianday(d.created_at)>=julianday(:since) AND julianday(d.created_at)<julianday(:until)
GROUP BY d.internal_event, d.reason, day
"""


def set_aside(db: sqlite3.Connection, since: str, until: str) -> list[dict]:
    """Return one row per event kind and reason: its total and its count per UTC day."""
    start, end = datetime.fromisoformat(since), datetime.fromisoformat(until)
    params = {
        "since": since, "until": until,
        "since_lo": (start - timedelta(days=1)).date().isoformat(),
        "until_hi": (end + timedelta(days=1)).date().isoformat(),
    }
    groups: dict[tuple[str, str], dict] = {}
    for row in db.execute(_SET_ASIDE_SQL, params):
        reason = row["reason"]
        key = (row["internal_event"], reason)
        group = groups.setdefault(key, {
            "internal_event": row["internal_event"], "reason": reason, "total": 0, "days": {},
        })
        group["total"] += row["n"]
        group["days"][row["day"]] = group["days"].get(row["day"], 0) + row["n"]
    rows = sorted(groups.values(), key=lambda g: (-g["total"], g["internal_event"], g["reason"]))
    for group in rows:
        group["days"] = dict(sorted(group["days"].items()))
    return rows
