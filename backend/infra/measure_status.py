"""What ``measure_status`` shows an agent: rows, counters, refusal texts and synced files."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from infra.measure_registry import REFUSAL_LIMIT, REGISTRY
from infra.otlp_state import REFUSALS

_WINDOW_DAYS = {"today": 0, "last_7_days": 6}
# Both reads are index ranges: observed_ts text starts with the UTC day, so a day string bounds it.
ROWS_SQL = (
    "SELECT event_name, count(*) FROM outcome_events "
    "WHERE agent_thread_id=? AND observed_ts >= ? GROUP BY event_name"
)
COUNTERS_SQL = (
    "SELECT event_name, counter, sum(n) FROM outcome_event_counters "
    "WHERE agent_thread_id=? AND day >= ? GROUP BY event_name, counter"
)
# The agent's definition rows, one primary-key range; the latest version per item is picked in Python.
DEFINITIONS_SQL = "SELECT kind, name, version, removed, file_path, file_sha256, synced_at FROM measure_definitions WHERE owner_thread_id=?"


def synced_files(db: sqlite3.Connection, agent_thread_id: str) -> list[dict[str, str]]:
    """The files that still define something live, each with its last sync, newest first."""
    latest: dict[tuple[str, str], tuple[int, bool, str]] = {}
    last_sync: dict[str, tuple[str, str]] = {}
    for kind, name, version, removed, path, sha, synced in db.execute(DEFINITIONS_SQL, (agent_thread_id,)):
        if (kind, name) not in latest or version > latest[(kind, name)][0]:
            latest[(kind, name)] = (version, bool(removed), path)
        if path not in last_sync or synced > last_sync[path][1]:
            last_sync[path] = (sha, synced)
    active = {path for _, removed, path in latest.values() if not removed}
    files = sorted(active, key=lambda path: last_sync[path][1], reverse=True)
    return [{"path": path, "sha256": last_sync[path][0], "synced_at": last_sync[path][1]} for path in files]


def status(db: sqlite3.Connection, agent_thread_id: str) -> dict[str, Any]:
    """Per event, today and over 7 days: rows recorded and each counter, plus the last refusals."""
    events: dict[str, dict[str, Any]] = {}
    today = datetime.now(timezone.utc).date()
    for window, back in _WINDOW_DAYS.items():
        since = (today - timedelta(days=back)).isoformat()
        for name, rows in db.execute(ROWS_SQL, (agent_thread_id, since)):
            events.setdefault(name, {})[window] = {"rows": rows, "counters": {}}
        for name, counter, total in db.execute(COUNTERS_SQL, (agent_thread_id, since)):
            slot = events.setdefault(name, {}).setdefault(window, {"rows": 0, "counters": {}})
            slot["counters"][counter] = total
    for per_event in events.values():
        for window in _WINDOW_DAYS:
            per_event.setdefault(window, {"rows": 0, "counters": {}})
    result: dict[str, Any] = {
        "events": {name: events[name] for name in sorted(events)},
        "recent_refusals": list(REGISTRY.refusals.get(agent_thread_id, ())),
        "refusals_note": f"The last {REFUSAL_LIMIT} refusal texts are held in memory and are lost when CORSIA restarts; the counters above are kept.",
        "synced_files": synced_files(db, agent_thread_id),
    }
    if request_refusals := REFUSALS.of(agent_thread_id):
        result["request_refusals"] = request_refusals
        result["request_refusals_note"] = (
            "Requests from outside tools refused before any record was read, by cause. "
            "Held in memory and lost when CORSIA restarts."
        )
    unversioned = sum(window["counters"].get("unversioned", 0) for per_event in events.values() for window in [per_event["last_7_days"]])
    if unversioned:
        result["unversioned"] = (
            f"{unversioned} rows unversioned in the last 7 days: their schedule names a workflow that is no longer yours, "
            "or whose version Git could not read, so they carry no workflow version. Register the schedule with a workflow "
            "you own (automation_list shows workflow_stale), or commit the workflow skill."
        )
    return result
