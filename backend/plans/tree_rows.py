"""Bounded raw loading of unarchived plan-node rows for read projections."""

from __future__ import annotations

import sqlite3

from infra.packed_rows import fetch_all
from plans.hierarchy import _ID_CHUNK
from plans.node_states import unarchived_sql


def many(connection, plan_ids) -> dict[str, list[sqlite3.Row]]:
    """Return each plan's unarchived tree rows in canonical ``order_key, id`` order."""
    ids = list(dict.fromkeys(str(plan_id) for plan_id in plan_ids))
    grouped: dict[str, list[sqlite3.Row]] = {plan_id: [] for plan_id in ids}
    for start in range(0, len(ids), _ID_CHUNK):
        chunk = ids[start:start + _ID_CHUNK]
        marks = ",".join("?" for _ in chunk)
        rows = fetch_all(
            connection, f"""SELECT * FROM plan_nodes
                WHERE plan_id IN ({marks}) AND {unarchived_sql('plan_nodes')}
                ORDER BY plan_id,order_key,id""",
            chunk,
        )
        for row in rows:
            grouped[str(row["plan_id"])].append(row)
    return grouped
