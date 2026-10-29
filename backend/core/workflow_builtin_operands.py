"""Workflow KPI operands read from the plan and Measurement's canonical ledgers."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
import sqlite3

from core.correction_attribution import measured_attributed_facts, plan_attributed_facts
from core.cost_identity import IdentityResolver
from core.measurement_pricing import classify_usage_rows, endpoint_listings, grouped_usage_sql
from core.measurement_scope import MeasurementScope, scope_cte
from core.workflow_verdict_read import _terminal_times


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


def read_builtin(db: sqlite3.Connection, *, builtin: str, workflow: str, user_id: str,
                 scope: MeasurementScope, start: str, end: str) -> Result:
    """Read a builtin in [start,end), grouped by its task's workflow_version.

    Scope is the caller's validated Measurement scope; timestamps are UTC instants.
    """
    if builtin not in {'runs', 'fresh_tokens', 'cost_usd', 'corrections', 'active_hours'}:
        raise ValueError('unknown workflow builtin')
    params = {**scope.params(user_id), 'workflow': workflow, 'start': start, 'end': end}
    nodes = db.execute(scope_cte() + """
        SELECT n.id,n.workflow_version FROM plan_nodes n JOIN plans p ON p.id=n.plan_id
        WHERE n.kind='task' AND n.workflow_ref=:workflow AND p.owner_kind='thread'
          AND p.owner_id IN (SELECT id FROM scope_threads)
    """, params).fetchall()
    if not nodes:
        return Result({}, 'not tracked: this workflow has no tasks')
    if builtin == 'active_hours':
        return Result({}, 'not tracked: presence is not recorded per task')
    versions = {r['id']: r['workflow_version'] for r in nodes}
    ids = json.dumps(list(versions))
    totals: defaultdict[str | None, list] = defaultdict(lambda: [0, 0, 0])
    if builtin == 'runs':
        first: dict[str, str] = {}
        for (node_id, _state), at in _terminal_times(db, list(versions), normalized=True).items():
            if at is not None and (node_id not in first or at < first[node_id]):
                first[node_id] = at
        entries = list(first.items())
        for index in _in_window(db, [at for _, at in entries], start, end):
            total = totals[versions[entries[index][0]]]
            total[0] += 1
            total[1] += 1
    elif builtin == 'corrections':
        facts = [fact for fact in (*measured_attributed_facts(db), *plan_attributed_facts(db))
                 if fact.node_id in versions and fact.is_correction and fact.source_at is not None]
        for index in _in_window(db, [fact.source_at for fact in facts], start, end):
            total = totals[versions[facts[index].node_id]]
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
            total = totals[versions[row['plan_node_id']]]
            total[0] += row['amount']
            total[1] += row['n']
            total[2] += row['unknown']
    else:
        sql = grouped_usage_sql({'node_id': 'u.plan_node_id'}, """llm_usage u
            WHERE u.plan_node_id IN (SELECT value FROM json_each(?))
              AND datetime(u.created_at)>=datetime(?) AND datetime(u.created_at)<datetime(?)""")
        rows = [dict(row) for row in db.execute(sql, (ids, start, end)).fetchall()]
        resolve = IdentityResolver(db)
        for row in rows:
            row['model'] = resolve(row).key
        for row in classify_usage_rows(rows, endpoint_listings(db)):
            total = totals[versions[row['node_id']]]
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
