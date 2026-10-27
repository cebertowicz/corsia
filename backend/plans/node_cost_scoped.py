"""Cost reads scoped to the nodes a read lists (#731 S2-1, S2-2), not the whole plan.

A scoped read loads the shape of everything it projects first: the listed nodes, the
subtrees it rolls, the plans attached beneath them, and each follow-up lineage with the
rows its total reads from outside it. Then it reads every one of those nodes' own facts,
and prices the ledger for them and the commitments asked, once; then it sums. However
many plans are in scope, no plan reads or prices its own cost apart from the others.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection
from datetime import datetime, timezone

from infra.packed_rows import fetch_all
from plans import hierarchy, node_cost, node_cost_initiative, node_time, service, usage_cost

_CHUNK = 400


_SUBTREE_SQL = f"""
    WITH RECURSIVE subtree(id) AS (
      SELECT id FROM plan_nodes WHERE id IN ({{marks}})
      UNION
      SELECT n.id FROM subtree s CROSS JOIN plan_nodes n ON n.plan_id=? AND n.parent_id=s.id
    )
    SELECT {node_cost.NODE_COLUMNS} FROM plan_nodes WHERE id IN (SELECT id FROM subtree)"""


def _chunks(ids: list[str]):
    for start in range(0, len(ids), _CHUNK):
        yield ids[start:start + _CHUNK]


def _subtree_rows(connection, plan_id: str, ids: list[str]) -> list[dict]:
    """These nodes and everything beneath them in their plan, archived included."""
    rows: dict[str, dict] = {}
    for chunk in _chunks(ids):
        sql = _SUBTREE_SQL.format(marks=",".join("?" for _ in chunk))
        rows.update({str(row["id"]): dict(row) for row in fetch_all(connection, sql, (*chunk, plan_id))})
    return list(rows.values())


def _by_plan(connection, node_ids) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    ids = sorted({str(value) for value in node_ids})
    for chunk in _chunks(ids):
        marks = ",".join("?" for _ in chunk)
        for row in fetch_all(connection, f"SELECT id,plan_id FROM plan_nodes WHERE id IN ({marks})", chunk):
            grouped[str(row["plan_id"])].append(str(row["id"]))
    return grouped


def _carried_plans(connection, plan_id: str, rows: list[dict]) -> tuple[dict[str, str], list[str], dict, dict]:
    """The plans these rows' steps carry: which step carries which, and every plan beneath, loaded.

    Returns the carrying steps, the carried plans' whole tree, its nodes by plan and its own
    attachments. Nothing here reads a cost.
    """
    in_rows = {str(row["id"]) for row in rows}
    carried = {
        step: child for step, child in node_cost._attached_children(connection, [plan_id]).items()
        if step in in_rows
    }
    if not carried:
        return {}, [], {}, {}
    tree = [str(row["id"]) for row in hierarchy.descendants(connection, sorted(set(carried.values())))]
    return carried, tree, node_cost._plan_nodes(connection, tree), node_cost._attached_children(connection, tree)


def _roll_shape(connection, plan_id: str, ids: list[str]) -> dict:
    """What rolling these nodes of one plan reads: their subtrees and the plans those carry.

    ``needs`` is every node whose own cost the roll sums.
    """
    rows = _subtree_rows(connection, plan_id, ids)
    carried, tree, child_nodes, child_attached = _carried_plans(connection, plan_id, rows)
    needs = [str(row["id"]) for row in [*rows, *(row for found in child_nodes.values() for row in found)]]
    return {"ids": ids, "rows": rows, "carried": carried, "tree": tree,
            "child_nodes": child_nodes, "child_attached": child_attached, "needs": needs}


def _linked(connection, ids: set[str]) -> set[str]:
    """The nodes joined to these by ``follow_up_of``, in either direction; an unlinked node is absent."""
    found: set[str] = set()
    listed = sorted(ids)
    for chunk in _chunks(listed):
        marks = ",".join("?" for _ in chunk)
        for row in fetch_all(
            connection,
            f"SELECT id,follow_up_of FROM plan_nodes WHERE follow_up_of IN ({marks}) "
            f"OR (id IN ({marks}) AND follow_up_of IS NOT NULL)",
            (*chunk, *chunk),
        ):
            found.add(str(row["id"]))
            if row["follow_up_of"] is not None:
                found.add(str(row["follow_up_of"]))
    return found


def _lineage_shape(connection, plan_id: str, ids: list[str]) -> dict | None:
    """What the initiative totals of these nodes of one plan read; None when none is in a lineage.

    The lineage of a node is its follow-up connected component, which never leaves its plan.
    That component and everything beneath its members is loaded, with the plans attached
    beneath them, and the same totals are computed over it as over the whole plan: a node's
    initiative does not depend on the rest of its plan's history.
    """
    members = _linked(connection, set(ids))
    if not members:
        return None
    while True:
        grown = members | _linked(connection, members)
        if grown == members:
            break
        members = grown
    if len(members) < 2:
        return None
    rows = _subtree_rows(connection, plan_id, sorted(members))
    carried, _tree, child_nodes, child_attached = _carried_plans(connection, plan_id, rows)
    expanded = node_cost_initiative.expand(
        connection, {plan_id: rows, **child_nodes}, {**carried, **child_attached},
    )
    if expanded is None:
        return None
    return {"ids": ids, "members": members, "expanded": expanded, "needs": node_cost_initiative.node_ids(expanded)}


def _roll(shape: dict, own: dict, spans: dict) -> dict[str, dict]:
    """Each asked node's rolled cost, settled, from its loaded shape and the read own facts."""
    totals: dict[str, dict] = {}
    plan_spans: dict[str, node_time.Span] = {}
    if shape["carried"]:
        _, totals, plan_spans = node_cost._roll_plans_with_totals(
            shape["tree"], shape["child_nodes"], own, shape["child_attached"], spans,
        )
    rolled, _span = node_cost._rollup_one_plan(
        shape["rows"], own, shape["carried"],
        {child: totals[child] for child in set(shape["carried"].values())}, spans, plan_spans,
    )
    return {node_id: node_cost._settled(rolled[node_id]) for node_id in shape["ids"]}


def project(
    listed, wanted, commitment_ids: Collection[str], *, db, now: datetime | None = None,
) -> tuple[dict[str, dict], dict[str, dict], dict[str, dict | None], usage_cost.Ledger]:
    """The cost figures of one scoped read, with one priced ledger read for all of them.

    Returns each ``listed`` node's own cost (settled, nothing rolled), each ``wanted``
    node's rolled cost and initiative total (None for one in no lineage), and the
    ``usage_cost.Ledger`` of ``commitment_ids`` for ``commitment_cost.project_many``.
    The cost of a node is its subtree and the plans its steps carry, never the rest of its
    plan: a leaf costs its own rows, a container its work (the limit S5 lifts).
    """
    clock = now or datetime.now(timezone.utc)
    listed = sorted({str(value) for value in listed})
    with service._connection(db) as connection:
        grouped = _by_plan(connection, wanted)
        rolls = [_roll_shape(connection, plan_id, ids) for plan_id, ids in grouped.items()]
        lineages = [
            shape for plan_id, ids in grouped.items()
            if (shape := _lineage_shape(connection, plan_id, ids)) is not None
        ]
        needed = sorted({*listed, *(node_id for shape in [*rolls, *lineages] for node_id in shape["needs"])})
        dollars, ledger = usage_cost.priced_with_pending(connection, needed, commitment_ids)
        own, spans = node_cost.own_many_timed(needed, db=connection, now=clock, dollars=dollars)
    rolled: dict[str, dict] = {}
    for shape in rolls:
        rolled.update(_roll(shape, own, spans))
    initiatives: dict[str, dict | None] = {str(value): None for value in wanted}
    for shape in lineages:
        totals = node_cost_initiative.totals(shape["expanded"], own)
        initiatives.update({node_id: totals.get(node_id) for node_id in shape["ids"] if node_id in shape["members"]})
    settled = {node_id: node_cost._settled(own[node_id]) for node_id in listed}
    return settled, rolled, initiatives, ledger
