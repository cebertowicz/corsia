"""Workflow KPI operands read from the plan and Measurement's canonical ledgers."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
import sqlite3
from typing import Any, NamedTuple

from core.correction_attribution import attributed_facts, workflow_run_of
from core.cost_identity import IdentityResolver
from core.measurement_pricing import classify_usage_rows, endpoint_listings, grouped_usage_sql
from core.measurement_scope import MeasurementScope, scope_cte


def _in_window(db: sqlite3.Connection, times: list[str | None], start: str, end: str) -> set[int]:
    """Compare all timestamps in one query using SQLite's datetime rules."""
    return {int(row[0]) for row in db.execute("""
        SELECT key FROM json_each(?)
        WHERE datetime(value)>=datetime(?) AND datetime(value)<datetime(?)
    """, (json.dumps(times), start, end))}


@dataclass(frozen=True)
class Value:
    value: float | int | None
    n: int
    unknown: int = 0
    reason: str | None = None


@dataclass(frozen=True)
class Result:
    versions: dict[str | None, Value]
    reason: str | None = None


class Prepared(NamedTuple):
    """A workflow's period-independent inputs, loaded once per request."""

    nodes: list
    times: dict
    ledger: list
    membership: dict[str, str]
    pricing: tuple[Any, Any] | None  # (identity resolver, endpoint listings) when cost_usd is read


def scoped_runs(db: sqlite3.Connection, user_id: str, scope: MeasurementScope) -> tuple:
    """Load scoped nodes and shared workflow roots."""
    from core.workflow_verdict_read import run_roots  # imports api.*, which imports this module

    rows = db.execute(scope_cte() + """
        SELECT n.id,n.parent_id,n.workflow_ref,n.workflow_version,n.created_at
        FROM plan_nodes n JOIN plans p ON p.id=n.plan_id
        WHERE p.owner_kind='thread' AND p.owner_id IN (SELECT id FROM scope_threads)
    """, scope.params(user_id)).fetchall()
    return rows, run_roots(rows)


def prepare_builtin(db: sqlite3.Connection, workflow: str, user_id: str, scope: MeasurementScope,
                    names: set[str], ledger=None, scoped=None) -> Prepared:
    """Load period-independent workflow inputs once per request."""
    from core.workflow_verdict_read import terminal_times

    rows, roots = scoped if scoped is not None else scoped_runs(db, user_id, scope)
    nodes = [row for row in roots if row['workflow_ref'] == workflow]
    root_ids = {row['id'] for row in roots}
    parents = {row['id']: row['parent_id'] for row in rows}
    wanted = {row['id'] for row in nodes}
    membership = {row['id']: run for row in rows
                  if (run := workflow_run_of(row['id'], parents, root_ids)) in wanted}
    times = terminal_times(db, list(wanted)) if nodes and 'runs' in names else {}
    if ledger is None and nodes and 'corrections' in names:
        ledger = attributed_facts(db)
    pricing = (IdentityResolver(db), endpoint_listings(db)) if nodes and 'cost_usd' in names else None
    return Prepared(nodes, times, ledger or [], membership, pricing)


def read_builtin(db: sqlite3.Connection, *, builtin: str, workflow: str, user_id: str,
                 scope: MeasurementScope, start: str, end: str, prepared: Prepared | None = None) -> Result:
    """Read a builtin in [start,end), grouped by its task's workflow_version."""
    if builtin not in {'runs', 'fresh_tokens', 'cost_usd', 'corrections', 'active_hours'}:
        raise ValueError('unknown workflow builtin')
    nodes, times, ledger, membership, pricing = (
        prepared if prepared is not None else prepare_builtin(db, workflow, user_id, scope, {builtin}))
    if not nodes:
        return Result({}, 'This workflow has no tasks')
    if builtin == 'active_hours':
        return Result({}, "Active time isn't recorded per task")
    versions = {r['id']: r['workflow_version'] for r in nodes}
    ids = json.dumps(list(membership))
    totals: defaultdict[str | None, list] = defaultdict(lambda: [0, 0, 0])
    if builtin == 'runs':
        first: dict[str, str] = {}
        for (node_id, _state), at in times.items():
            if at is not None and (node_id not in first or at < first[node_id]):
                first[node_id] = at
        entries = list(first.items())
        for index in _in_window(db, [at for _, at in entries], start, end):
            total = totals[versions[entries[index][0]]]
            total[0] += 1
            total[1] += 1
    elif builtin == 'corrections':
        facts = [fact for fact in ledger
                 if fact.node_id in membership and fact.is_correction and fact.source_at is not None]
        for index in _in_window(db, [fact.source_at for fact in facts], start, end):
            total = totals[versions[membership[facts[index].node_id]]]
            total[0] += 1
            total[1] += 1
    elif builtin == 'fresh_tokens':
        from api.measurement_token_sql import FRESH_TOKEN_SQL, FRESH_TOKEN_UNKNOWN_SQL

        rows = db.execute(f"""
            SELECT u.plan_node_id,SUM({FRESH_TOKEN_SQL}) AS amount,COUNT(*) AS n,
                   SUM({FRESH_TOKEN_UNKNOWN_SQL}) AS unknown FROM llm_usage u
            WHERE u.plan_node_id IN (SELECT value FROM json_each(?))
              AND datetime(u.created_at)>=datetime(?) AND datetime(u.created_at)<datetime(?)
            GROUP BY u.plan_node_id
        """, (ids, start, end)).fetchall()
        for row in rows:
            total = totals[versions[membership[row['plan_node_id']]]]
            total[0] += row['amount']
            total[1] += row['n']
            total[2] += row['unknown']
    else:
        sql = grouped_usage_sql({'node_id': 'u.plan_node_id'}, """llm_usage u
            WHERE u.plan_node_id IN (SELECT value FROM json_each(?))
              AND datetime(u.created_at)>=datetime(?) AND datetime(u.created_at)<datetime(?)""")
        rows = [dict(row) for row in db.execute(sql, (ids, start, end)).fetchall()]
        resolve, listings = pricing or (IdentityResolver(db), endpoint_listings(db))
        for row in rows:
            row['model'] = resolve(row).key
        for row in classify_usage_rows(rows, listings):
            total = totals[versions[membership[row['node_id']]]]
            total[1] += row['records']
            if row['outcome'] == 'priced':
                total[0] += row['dollars']
            elif row['outcome'] != 'not_api_priced':
                total[2] += row['records']
    for version in versions.values():
        totals[version]
    return Result({version: Value(None if builtin == 'cost_usd' and amount[1] and amount[1] == amount[2]
                                  else amount[0], amount[1], amount[2])
                   for version, amount in totals.items()})
