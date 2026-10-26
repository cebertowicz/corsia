"""What ``list`` shows: the project's rules of both kinds, and the shipped rule sets' rows. Names no connector."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from events import observe_rules, rules

_STARTS = "'queued','running','ran','failed','started','returned','invalid','no_result'"
_ACTED = "'woke','digested','dropped','registered','not_registered'"


def shape(rule: rules.Rule) -> dict[str, Any]:
    return {
        "id": rule.id, "kind": rules.ROUTE, "enabled": rule.enabled, "connector": rule.connector,
        "event_type": rule.event_type, "match": list(rule.match), "action": rule.action,
        "to": rule.to, "mode": rule.mode, "note": rule.note, "written_by": rule.created_by,
        "script_id": rule.script_id, "workflow_id": rule.workflow_id,
        "request_identity_fact": rule.request_identity_fact,
    }


def _shaped(row: sqlite3.Row) -> dict[str, Any]:
    if row["kind"] == observe_rules.OBSERVE:
        return observe_rules.shape(observe_rules.from_row(row))
    return shape(rules.from_row(row))


def _stats(db: sqlite3.Connection, ids: list[str], now: str) -> dict[str, sqlite3.Row]:
    since = (datetime.fromisoformat(now) - timedelta(hours=24)).isoformat()
    marks = ",".join("?" * len(ids))
    # A start is a script run or a worker start, queued or begun.
    started = f"outcome IN ({_STARTS})"
    return {
        row["rule_id"]: row for row in db.execute(
            f"SELECT rule_id, MAX(CASE WHEN outcome IN ({_ACTED}) OR "
            f"{started} THEN at END) AS last_fired, SUM(outcome='woke' AND via='rule' AND at>=?) "
            f"AS wakes, SUM({started} AND at>=?) AS starts, "
            "SUM(outcome='skipped' AND at>=?) AS skips "
            f"FROM event_route_decisions WHERE rule_id IN ({marks}) GROUP BY rule_id",
            (since, since, since, *ids),
        ).fetchall()
    }


def _with_stats(db: sqlite3.Connection, found: list[sqlite3.Row], now: str) -> list[dict[str, Any]]:
    if not found:
        return []
    stats = _stats(db, [row["id"] for row in found], now)
    return [
        {**_shaped(row),
         "last_fired": stats[row["id"]]["last_fired"] if row["id"] in stats else None,
         "wakes_24h": int(stats[row["id"]]["wakes"] or 0) if row["id"] in stats else 0,
         "starts_24h": int(stats[row["id"]]["starts"] or 0) if row["id"] in stats else 0,
         "skips_24h": int(stats[row["id"]]["skips"] or 0) if row["id"] in stats else 0}
        for row in found
    ]


def list_rules(db: sqlite3.Connection, *, project_id: str | None, now: str) -> list[dict[str, Any]]:
    """The project's rules of both kinds (disabled ones only within retention), last action, and
    24h wakes, script starts and skipped runs."""
    if not project_id:
        return []
    cutoff = (datetime.fromisoformat(now) - rules.RETENTION).isoformat()
    found = db.execute(
        "SELECT * FROM event_rules WHERE scope=? AND (enabled=1 OR updated_at>=?) "
        "ORDER BY created_at, id",
        (rules.project_scope(project_id), cutoff),
    ).fetchall()
    return _with_stats(db, found, now)


def rule_set_rows(db: sqlite3.Connection, *, now: str) -> list[dict[str, Any]]:
    """The shipped rule sets' rows, which apply to every caller in every project."""
    found = db.execute(
        "SELECT * FROM event_rules WHERE scope=? AND enabled=1 ORDER BY created_at, id",
        (observe_rules.SHIPPED_SCOPE,),
    ).fetchall()
    return _with_stats(db, found, now)
