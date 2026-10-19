"""What ``measure_status`` shows an agent: rows, counters, refusal texts and synced files."""
from __future__ import annotations

import sqlite3
from typing import Any

from infra.measure_registry import REFUSAL_LIMIT, REGISTRY

_WINDOWS = {"today": "date('now')", "last_7_days": "date('now','-6 days')"}


def status(db: sqlite3.Connection, agent_thread_id: str) -> dict[str, Any]:
    """Per event, today and over 7 days: rows recorded and each counter, plus the last refusals."""
    events: dict[str, dict[str, Any]] = {}
    for window, since in _WINDOWS.items():
        for name, rows in db.execute(
            f"SELECT event_name, count(*) FROM outcome_events WHERE agent_thread_id=? AND substr(observed_ts,1,10) >= {since} GROUP BY event_name",
            (agent_thread_id,),
        ):
            events.setdefault(name, {})[window] = {"rows": rows, "counters": {}}
        for name, counter, total in db.execute(
            f"SELECT event_name, counter, sum(n) FROM outcome_event_counters WHERE agent_thread_id=? AND day >= {since} GROUP BY event_name, counter",
            (agent_thread_id,),
        ):
            slot = events.setdefault(name, {}).setdefault(window, {"rows": 0, "counters": {}})
            slot["counters"][counter] = total
    for per_event in events.values():
        for window in _WINDOWS:
            per_event.setdefault(window, {"rows": 0, "counters": {}})
    files = db.execute(
        "SELECT file_path, file_sha256, max(synced_at) FROM measure_definitions WHERE owner_thread_id=? GROUP BY file_path, file_sha256 ORDER BY max(synced_at) DESC",
        (agent_thread_id,),
    ).fetchall()
    result: dict[str, Any] = {
        "events": {name: events[name] for name in sorted(events)},
        "recent_refusals": list(REGISTRY.refusals.get(agent_thread_id, ())),
        "refusals_note": f"The last {REFUSAL_LIMIT} refusal texts are held in memory and are lost when CORSIA restarts; the counters above are kept.",
        "synced_files": [{"path": path, "sha256": sha, "synced_at": synced} for path, sha, synced in files],
    }
    unversioned = sum(window["counters"].get("unversioned", 0) for per_event in events.values() for window in [per_event["last_7_days"]])
    if unversioned:
        result["unversioned"] = (
            f"{unversioned} rows unversioned in the last 7 days: their schedule's workflow is no longer yours, "
            "so they carry no workflow version. Sync that workflow's measures.yaml again, or register the schedule with a workflow you own."
        )
    return result
