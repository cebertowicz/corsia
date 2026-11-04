"""Priced model cost of whole threads, for the KPI ``cost_of`` operand."""
from __future__ import annotations

import json
import sqlite3
from typing import Iterable

from core.cost_identity import IdentityResolver
from core.measurement_pricing import classify_usage_rows, endpoint_listings, grouped_usage_sql


def thread_costs(db: sqlite3.Connection, user_id: str, thread_ids: Iterable[str]) -> dict[str, float | None]:
    """Each thread's whole-life priced cost in USD, or None when any of its usage can't be priced.

    One read for every id, through the one pricing path ``cost_usd`` uses. Only the requesting user's threads
    are read. A thread with no usage rows, or one the user doesn't own, is absent: its cost is unknown.
    """
    ids = sorted(set(thread_ids))
    if not ids:
        return {}
    sql = grouped_usage_sql({'thread_id': 'u.thread_id'}, """llm_usage u
        WHERE u.thread_id IN (SELECT value FROM json_each(?))
          AND u.thread_id IN (SELECT id FROM threads WHERE user_id=?)""")
    rows = [dict(row) for row in db.execute(sql, (json.dumps(ids), user_id)).fetchall()]
    resolve = IdentityResolver(db)
    for row in rows:
        row['model'] = resolve(row).key
    costs: dict[str, float | None] = {}
    for row in classify_usage_rows(rows, endpoint_listings(db)):
        thread = row['thread_id']
        so_far = costs.get(thread, 0.0)
        if row['outcome'] == 'priced':
            costs[thread] = None if so_far is None else so_far + row['dollars']
        elif row['outcome'] == 'not_api_priced':  # not billed per call, as in cost_usd: adds nothing, not unknown
            costs[thread] = so_far
        else:
            costs[thread] = None
    return costs
