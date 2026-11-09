"""The same open-work read as ``open_tree.load``, for many plans in one round trip.

``open_tree.load`` costs one plan read and one open-row read per plan. A read of one
plan pays that once; the chat list pays it once per thread it shows, and every SQLite
step re-takes the GIL, so beside a busy process hundreds of small statements cost
seconds while the same rows in one statement cost milliseconds. Here the open rows of
a whole chunk of plans arrive in one read, and each plan's tree is then built exactly
as ``load`` builds one.

Only the read that lists no folded rows and no recent-settled window takes this path:
those two walks are per plan by nature (a fold window is per parent, a recent window is
a cutoff walk), so a pane read still costs one ``load`` each. ``open_rollup.rollup_plans``
chooses between them.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from infra.packed_rows import fetch_all
from plans import errors, node_states
from plans.open_tree import OpenTree, _chunks, _fetch_missing, _order, _shape

_OPEN_MANY_SQL = (
    f"SELECT * FROM plan_nodes WHERE plan_id IN ({{marks}}) AND "
    f"{node_states.listed_open_sql('plan_nodes')}"
)


def load_many(connection, plan_ids: Iterable[str]) -> dict[str, OpenTree]:
    """Each named plan's open rows and ancestors, in one statement per chunk of plans.

    The same rows, order and ``Shape`` as calling ``load`` on each plan; one plan read
    and one open-row read per ``_ID_CHUNK`` of plans replaces one pair per plan. Raises
    ``errors.UnknownPlan`` for an id no plan owns, as ``load`` does.
    """
    ids = list(dict.fromkeys(str(plan_id) for plan_id in plan_ids))
    plans: dict[str, sqlite3.Row] = {}
    for chunk in _chunks(ids):
        marks = ",".join("?" for _ in chunk)
        for row in fetch_all(connection, f"SELECT * FROM plans WHERE id IN ({marks})", chunk):
            plans[str(row["id"])] = row
    by_plan: dict[str, dict[str, sqlite3.Row]] = {plan_id: {} for plan_id in ids}
    for chunk in _chunks(ids):
        marks = ",".join("?" for _ in chunk)
        for row in fetch_all(connection, _OPEN_MANY_SQL.format(marks=marks), chunk):
            by_plan[str(row["plan_id"])][str(row["id"])] = row
    trees: dict[str, OpenTree] = {}
    for plan_id in ids:
        plan = plans.get(plan_id)
        if plan is None:
            raise errors.UnknownPlan(f"unknown plan: {plan_id}")
        found = by_plan[plan_id]
        _fetch_missing(connection, found, [row["parent_id"] for row in list(found.values())])
        trees[plan_id] = OpenTree(plan=plan, listed=tuple(_order(found)), shape=_shape(found))
    return trees
