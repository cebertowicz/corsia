"""The set_aside audit metric: events set aside instead of delivered, per day, by kind and reason.

Read through ``infra.audit_reads``, which binds the caller's scope and deadline.
An event is set aside when ``events.dead_letter.park`` (or the restart webhook
replay) writes a ``dead_letter_events`` row instead of waking an agent. The
read returns counts only: no payload, external id or thread.

The read is install-wide and rows carry no owner, so a kind is shown by name
only when code names it: a built-in connector's manifest, the GitHub routing
map, or a kind the platform parks itself. Any other name was written by a
person or an agent (a declared event type, a script's event) and is counted
under ``DECLARED`` instead, so no project's or user's names cross to another.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from functools import cache

#: The one row name every kind not named in code is counted under.
DECLARED = "declared"
#: Kinds the platform parks under a literal name of its own (bulletin_live,
#: script_runner, watches, github_pr_quiet_watch; failed_wake_surface's
#: REPORT_EVENT is added below). A name missing here is counted under
#: DECLARED, never shown.
_PLATFORM_KINDS = frozenset({
    "system_bulletin.infrastructure", "script.owner_gone",
    "automation.watch_expired", "github.ingress_overdue",
})


@cache
def _code_named() -> frozenset[str]:
    # Imported here so loading the audit reads does not load the event stack.
    from events.connectors import get_manifest, registered_connectors
    from events.connectors.github import GITHUB_ROUTED_EVENTS
    from infra.failed_wake_surface import REPORT_EVENT

    manifest_kinds = {
        f"{connector}.{event_type}"
        for connector in registered_connectors()
        for event_type in get_manifest(connector).event_types
    }
    return frozenset(
        manifest_kinds | set(GITHUB_ROUTED_EVENTS.values()) | _PLATFORM_KINDS | {REPORT_EVENT}
    )

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
        kind = row["internal_event"] if row["internal_event"] in _code_named() else DECLARED
        key = (kind, reason)
        group = groups.setdefault(key, {
            "internal_event": kind, "reason": reason, "total": 0, "days": {},
        })
        group["total"] += row["n"]
        group["days"][row["day"]] = group["days"].get(row["day"], 0) + row["n"]
    rows = sorted(groups.values(), key=lambda g: (-g["total"], g["internal_event"], g["reason"]))
    for group in rows:
        group["days"] = dict(sorted(group["days"].items()))
    return rows
