"""Cost reads scoped to the nodes a read lists (#731 S2-1, S2-2), not the whole plan."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from infra.packed_rows import fetch_all
from plans import hierarchy, node_cost, node_cost_initiative, node_time, service

_CHUNK = 400


Known = tuple[dict[str, dict], dict[str, node_time.Span]]


def own_timed(node_ids, *, db, now: datetime | None = None) -> Known:
    """These nodes' own cost facts and time intervals, unsettled: what a later roll reuses."""
    ids = sorted({str(value) for value in node_ids})
    with service._connection(db) as connection:
        return node_cost.own_many_timed(ids, db=connection, now=now)


def own_listed(node_ids, *, db, now: datetime | None = None, known: Known | None = None) -> dict[str, dict]:
    """Each of these nodes' own cost, settled, and nothing rolled: the cost of the ids asked."""
    own = (known or own_timed(node_ids, db=db, now=now))[0]
    return {str(node_id): node_cost._settled(own[str(node_id)]) for node_id in node_ids}


def _own_with(connection, ids: list[str], clock: datetime, known: Known | None) -> Known:
    """Own facts for ``ids``: ``known`` ones reused, the rest read in one batch."""
    wanted = set(ids)
    own = {k: v for k, v in (known[0] if known else {}).items() if k in wanted}
    spans = {k: v for k, v in (known[1] if known else {}).items() if k in wanted}
    missing = [node_id for node_id in ids if node_id not in own]
    if missing:
        more_own, more_spans = node_cost.own_many_timed(missing, db=connection, now=clock)
        own.update(more_own)
        spans.update(more_spans)
    return own, spans


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


def rolled_many(
    node_ids, *, db, now: datetime | None = None, known: Known | None = None,
) -> dict[str, dict]:
    """Each node's rolled cost, read from the subtrees of these nodes alone (archived nodes included).

    The cost of a node is its subtree and the plans its steps carry, never the rest of its
    plan: a leaf costs its own rows, a container its work (the limit S5 lifts). All the
    nodes of one plan are rolled in one pass over the union of their subtrees. ``known`` is
    own cost facts already read (``own_timed``), reused rather than read again.
    """
    clock = now or datetime.now(timezone.utc)
    found: dict[str, dict] = {}
    with service._connection(db) as connection:
        for plan_id, ids in _by_plan(connection, node_ids).items():
            rows = _subtree_rows(connection, plan_id, ids)
            ids_in = [str(row["id"]) for row in rows]
            in_subtree = set(ids_in)
            carried = {
                step: child for step, child in node_cost._attached_children(connection, [plan_id]).items()
                if step in in_subtree
            }
            totals: dict[str, dict] = {}
            plan_spans: dict[str, node_time.Span] = {}
            if carried:
                child_tree = [
                    str(row["id"])
                    for row in hierarchy.descendants(connection, sorted(set(carried.values())))
                ]
                loaded = node_cost._load_plans(connection, child_tree, clock)
                _, totals, plan_spans = node_cost._roll_plans_with_totals(child_tree, *loaded)
            own, spans = _own_with(connection, ids_in, clock, known)
            rolled, _span = node_cost._rollup_one_plan(
                rows, own, carried, {child: totals[child] for child in set(carried.values())},
                spans, plan_spans,
            )
            found.update({node_id: node_cost._settled(rolled[node_id]) for node_id in ids})
    return found


def rolled_of(node_id: str, *, db, now: datetime | None = None) -> dict:
    """One node's rolled cost (``rolled_many`` of one)."""
    return rolled_many([node_id], db=db, now=now)[node_id]


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


def initiatives_of(
    node_ids, *, db, now: datetime | None = None, known: Known | None = None,
) -> dict[str, dict | None]:
    """The initiative total (``node_cost_initiative``) of each of these nodes, None for one in no lineage.

    The lineage of a node is its follow-up connected component, which never leaves its plan.
    That component and everything beneath its members is loaded, with the plans attached
    beneath them, and the same totals are computed over it as over the whole plan: a node's
    initiative does not depend on the rest of its plan's history.
    """
    clock = now or datetime.now(timezone.utc)
    found: dict[str, dict | None] = {str(value): None for value in node_ids}
    with service._connection(db) as connection:
        for plan_id, ids in _by_plan(connection, node_ids).items():
            members = _linked(connection, set(ids))
            if not members:
                continue
            while True:
                grown = members | _linked(connection, members)
                if grown == members:
                    break
                members = grown
            if len(members) < 2:
                continue
            rows = _subtree_rows(connection, plan_id, sorted(members))
            row_ids = {str(row["id"]) for row in rows}
            attached = {
                step: child for step, child in node_cost._attached_children(connection, [plan_id]).items()
                if step in row_ids
            }
            nodes_by_plan: dict[str, list[dict]] = {plan_id: rows}
            own = _own_with(connection, sorted(row_ids), clock, known)[0]
            if attached:
                child_tree = [
                    str(row["id"]) for row in hierarchy.descendants(connection, sorted(set(attached.values())))
                ]
                child_nodes, child_own, child_attached, _spans = node_cost._load_plans(connection, child_tree, clock)
                nodes_by_plan.update(child_nodes)
                own.update(child_own)
                attached = {**attached, **child_attached}
            totals = node_cost_initiative.totals(connection, (nodes_by_plan, own, attached), clock)
            found.update({node_id: totals.get(node_id) for node_id in ids if node_id in members})
    return found
