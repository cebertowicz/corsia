"""The reads rule evaluation makes: the rules for an event, then threads and wake budget.

Each is one indexed read. The rules read is the one every event pays; the
others are made only after ``events.rules`` found a matching rule, so an event
no rule matches never pays them. The artifact's owner and lifecycle are
routing's, never read again here. An event routing gave no thread first pays
one read for a self-rule of its type; only then are the projects tracking its
repository read (a policy query and git). Names no connector.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from infra.database import get_db
from infra.queries import thread_available_sql


def enabled_rules(
    db: sqlite3.Connection, connector: str, event_type: str, prefix: str,
    thread: str | None, scopes: list[str],
) -> list[sqlite3.Row]:
    """The enabled rules for one declared type in ``thread``'s project (scope
    ``prefix`` + its project), else in ``scopes``: one read through the
    ``(connector, event_type, enabled, scope, created_at)`` index."""
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


def unrouted_projects(connector: str, event_type: str, facts: Mapping[str, Any]) -> frozenset[str]:
    """The projects tracking the repository of an event routing gave no thread.

    Only a writer's deliver rule to itself can fire there, so one read through
    the ``(connector, event_type, enabled, ...)`` index first asks whether any
    exists for the type, in any project. With none, the connector's repository
    lookup (a policy query and git config per registered repository) never runs.
    Runs git, so call it outside any write transaction.
    """
    from events.connectors import get_manifest

    manifest = get_manifest(connector)
    repository = facts.get(manifest.repository_fact or "")
    if manifest.repository_projects is None or not repository:
        return frozenset()
    db = get_db()
    try:
        if db.execute(
                "SELECT 1 FROM event_rules WHERE connector=? AND event_type=? AND enabled=1 "
                "AND action='deliver' AND \"to\"='thread:' || created_by LIMIT 1",
                (connector, event_type)).fetchone() is None:
            return frozenset()
    finally:
        db.close()
    return manifest.repository_projects(str(repository)) or frozenset()


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
