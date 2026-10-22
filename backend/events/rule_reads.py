"""The reads rule evaluation makes: the rules for an event, then threads and wake budget.

Each is one indexed read. The rules read is the one every event pays; the
others are made only after ``events.rules`` found a matching rule, so an event
no rule matches never pays them. The artifact's owner and lifecycle are
routing's, never read again here. A webhook pays its one read before routing's
ordered preparation (:func:`type_rules`), and none in it when no rule for its
type is enabled; only with a self-rule of its type are the projects tracking
its repository read (a policy query and git). Names no connector.
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


_TYPE_RULES = "FROM event_rules WHERE connector=? AND event_type=? AND enabled=1"
_SELF_RULES = f"{_TYPE_RULES} AND action='deliver' AND \"to\"='thread:' || created_by"


def self_rules(db: sqlite3.Connection, connector: str, event_type: str) -> list[sqlite3.Row]:
    """Every enabled deliver rule to its own writer for one type, in any project."""
    return db.execute(f"SELECT * {_SELF_RULES} ORDER BY created_at, id",
                      (connector, event_type)).fetchall()


def type_rules(connector: str, event_type: str) -> tuple[bool, bool]:
    """Whether any rule for one type is enabled, and any self-rule, in any project:
    one read, two probes of the ``(connector, event_type, enabled, ...)`` index."""
    db = get_db()
    try:
        row = db.execute(f"SELECT EXISTS(SELECT 1 {_TYPE_RULES}), EXISTS(SELECT 1 {_SELF_RULES})",
                         (connector, event_type) * 2).fetchone()
    finally:
        db.close()
    return bool(row[0]), bool(row[1])


def unrouted_projects(
    connector: str, event_type: str, facts: Mapping[str, Any], self_rule: bool | None = None,
) -> frozenset[str] | None:
    """The projects tracking the event's repository, should routing give it no thread.

    Only a writer's deliver rule to itself can fire there, so :func:`type_rules`
    first asks whether any exists for the type, unless the caller's ``self_rule``
    already says. With none, the connector's repository lookup (a policy query
    and git config per registered repository) never runs. ``None``: the lookup
    failed, which is not the same as an untracked repository. Runs git, so call
    it outside any write transaction and the preparation gate.
    """
    from events.connectors import get_manifest

    manifest = get_manifest(connector)
    repository = facts.get(manifest.repository_fact or "")
    if manifest.repository_projects is None or not repository:
        return frozenset()
    if not (type_rules(connector, event_type)[1] if self_rule is None else self_rule):
        return frozenset()
    return manifest.repository_projects(str(repository))


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


def wake_budgets(
    db: sqlite3.Connection, rule_ids: list[str], thread: str, *, cap: str, since: str,
) -> dict[str, tuple[int, int]]:
    """R9 for each of ``rule_ids`` on one thread, in one read: its wakes of the
    thread since ``since``, and how many of its events the cap has digested there
    since its last wake. A rule with neither is absent."""
    params = {"thread": thread, "cap": cap, "since": since,
              **{f"r{i}": rule_id for i, rule_id in enumerate(rule_ids)}}
    ids = ",".join(f":r{i}" for i in range(len(rule_ids)))
    rows = db.execute(
        "SELECT d.rule_id, "
        "       SUM(d.outcome='woke' AND d.via='rule' AND d.at>=:since) AS spent, "
        "       SUM(d.outcome='digested' AND d.reason=:cap AND d.id>COALESCE(w.last, 0)) AS capped "
        "FROM event_route_decisions d LEFT JOIN ("
        "    SELECT rule_id, MAX(id) AS last FROM event_route_decisions "
        f"   WHERE rule_id IN ({ids}) AND thread_id=:thread AND outcome='woke' AND via='rule' "
        "    GROUP BY rule_id) w ON w.rule_id=d.rule_id "
        f"WHERE d.rule_id IN ({ids}) AND d.thread_id=:thread AND d.outcome IN ('woke','digested') "
        "GROUP BY d.rule_id",
        params,
    ).fetchall()
    return {row["rule_id"]: (int(row["spent"]), int(row["capped"])) for row in rows}
