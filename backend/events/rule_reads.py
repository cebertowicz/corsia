"""The reads rule evaluation makes: the rules for an event, then threads and wake budget.

Each is one indexed read. The rules read is the one every event pays; the
others are made only after ``events.rules`` found a matching rule, so an event
no rule matches never pays them. The artifact's owner and lifecycle are
routing's, never read again here. Names no connector.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from infra.queries import thread_available_sql


def enabled_rules(
    db: sqlite3.Connection, connector: str, event_type: str, prefix: str,
    thread: str | None, scopes: list[str],
) -> list[sqlite3.Row]:
    """The enabled rules for one declared type in ``thread``'s project (scope
    ``prefix`` + its project), else in ``scopes``: one read through the
    ``(scope, connector, event_type, enabled)`` index."""
    if thread is not None:
        where, params = "scope=? || (SELECT project_id FROM threads WHERE id=?)", [prefix, thread]
    elif scopes:
        where, params = f"scope IN ({','.join('?' * len(scopes))})", list(scopes)
    else:
        return []
    return db.execute(
        f"SELECT * FROM event_rules WHERE {where} AND connector=? AND event_type=? "
        "AND enabled=1 ORDER BY created_at, id",
        [*params, connector, event_type],
    ).fetchall()


def threads(db: sqlite3.Connection, ids: Iterable[str | None]) -> dict[str, Mapping[str, Any]]:
    """Each named thread's project, parent and availability, in one read."""
    wanted = sorted({thread for thread in ids if thread})
    if not wanted:
        return {}
    rows = db.execute(
        f"SELECT id, project_id, spawned_by, {thread_available_sql()} AS available "
        f"FROM threads WHERE id IN ({','.join('?' * len(wanted))})",
        wanted,
    ).fetchall()
    return {row["id"]: row for row in rows}


def wake_budget(
    db: sqlite3.Connection, rule_id: str, thread: str, *, cap: str, since: str,
) -> tuple[int, int]:
    """R9: this rule's wakes of this thread since ``since``, and how many of its
    events the cap has digested there since its last wake, in one read."""
    row = db.execute(
        "SELECT (SELECT COUNT(*) FROM event_route_decisions WHERE rule_id=:rule "
        "        AND thread_id=:thread AND outcome='woke' AND via='rule' AND at>=:since) AS spent, "
        "       (SELECT COUNT(*) FROM event_route_decisions WHERE rule_id=:rule "
        "        AND thread_id=:thread AND outcome='digested' AND reason=:cap "
        "        AND id>COALESCE((SELECT MAX(id) FROM event_route_decisions WHERE rule_id=:rule "
        "                AND thread_id=:thread AND outcome='woke' AND via='rule'), 0)) AS capped",
        {"rule": rule_id, "thread": thread, "cap": cap, "since": since},
    ).fetchone()
    return int(row["spent"]), int(row["capped"])
