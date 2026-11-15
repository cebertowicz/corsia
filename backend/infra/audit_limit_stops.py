"""The limit_stops audit metric: how often each account reached a usage limit, and for how long.

Read through ``infra.audit_reads``, which binds the caller's scope and deadline.
``connections.limit_history`` writes one ``limit_observations`` row per change
of a window's reading, so a reading holds until the next row for its window or
until the window resets, whichever comes first.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

# Timestamps come from vendors and from CORSIA in more than one ISO form, so
# the text bounds only narrow the index range (a day wide on each side) and
# julianday() decides. A reading before the window is the state the window
# opens in: it counts toward hours at the limit, never as a stop.
_LIMIT_STOPS_SQL = """
WITH windows AS (
    SELECT DISTINCT account_id, window_key FROM limit_observations
), seed AS (
    SELECT o.* FROM windows w JOIN limit_observations o ON o.id = (
        SELECT p.id FROM limit_observations p
        WHERE p.account_id=w.account_id AND p.window_key=w.window_key
          AND p.observed_at<:since_hi AND julianday(p.observed_at)<:since_j
        ORDER BY p.observed_at DESC, p.id DESC LIMIT 1)
), readings AS (
    SELECT o.*, 0 AS opening FROM limit_observations o
    WHERE o.observed_at>=:since_lo AND o.observed_at<:until_hi
      AND julianday(o.observed_at)>=:since_j AND julianday(o.observed_at)<:until_j
    UNION ALL SELECT seed.*, 1 FROM seed
), ordered AS (
    SELECT account_id, window_key, window_kind, used_percent, opening,
           julianday(observed_at) AS t, julianday(resets_at) AS reset_t,
           LAG(used_percent) OVER w AS prev_percent,
           LAG(julianday(resets_at)) OVER w AS prev_reset_t,
           LEAD(julianday(observed_at)) OVER w AS next_t
    FROM readings
    WINDOW w AS (PARTITION BY account_id, window_key ORDER BY julianday(observed_at), id)
)
SELECT o.account_id, o.window_key, max(o.window_kind) AS window_kind,
       coalesce(sum(o.opening=0),0) AS observations,
       coalesce(sum(o.opening=0 AND o.used_percent>=100 AND (
           o.prev_percent IS NULL OR o.prev_percent<100
           OR (o.prev_reset_t IS NOT NULL AND o.prev_reset_t<=o.t))),0) AS stops,
       coalesce(sum(CASE WHEN o.used_percent>=100 THEN max(0,
           min(coalesce(o.next_t,:until_j), coalesce(o.reset_t,:until_j), :until_j)
           - max(o.t,:since_j)) END),0) * 24 AS hours_at_limit
FROM ordered o
GROUP BY o.account_id, o.window_key
ORDER BY o.account_id, o.window_key
"""


def limit_stops(db: sqlite3.Connection, since: str, until: str) -> list[dict]:
    """Return one row per account, each listing its windows' stops and hours at 100%.

    A window appears once it has any reading before the window's end;
    ``observations`` counts the readings inside, so a window that never
    changed reads as zero observations rather than going missing.
    """
    start, end = datetime.fromisoformat(since), datetime.fromisoformat(until)
    params = {
        "since_j": _julian(db, start), "until_j": _julian(db, end),
        "since_lo": (start - timedelta(days=1)).date().isoformat(),
        "since_hi": (start + timedelta(days=1)).date().isoformat(),
        "until_hi": (end + timedelta(days=1)).date().isoformat(),
    }
    # Names come from their own read: the stops query uses row wildcards, and
    # no statement touching vendor_accounts may (harness_usage owns usage).
    names = {row["id"]: row["name"] for row in db.execute("SELECT id, name FROM vendor_accounts")}
    accounts: dict[str, dict] = {}
    for row in db.execute(_LIMIT_STOPS_SQL, params):
        account = accounts.setdefault(row["account_id"], {
            "account_id": row["account_id"], "account_name": names.get(row["account_id"]),
            "windows": [],
        })
        account["windows"].append({
            "window_key": row["window_key"], "window_kind": row["window_kind"],
            "observations": row["observations"], "stops": row["stops"],
            "hours_at_limit": round(row["hours_at_limit"], 2),
        })
    return list(accounts.values())


def _julian(db: sqlite3.Connection, moment: datetime) -> float:
    return db.execute("SELECT julianday(?)", (moment.isoformat(),)).fetchone()[0]
