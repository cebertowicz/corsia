"""The canonical projection a status read is cut from, read once per call.

``facts`` reads what the plan holds open (``plans.open_tree``) and ``full_facts`` is the
same projection over every node, the reference the equivalence test holds it to.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from plans import (
    node_states, open_rollup, open_tree, operator_node_state, operator_nodes,
    operator_projection, rollup,
)
from plans.status_time import parse as _parse

RECENT_DAYS = 30


def facts(
    db, plan_row, actor, clock: str, *, detail_id: str | None = None, costs: bool = True,
) -> dict[str, Any]:
    """The projection of the open work, its recent window and the node asked about.

    Reads what the plan holds open (``plans.open_tree``), so the cost follows the open
    work and not the finished history behind it. ``full_facts`` is the same projection
    over every node, the reference the equivalence test holds this one to.
    """
    plan_id = str(plan_row["id"])
    cutoff = (_parse(clock) - timedelta(days=RECENT_DAYS)).isoformat()
    tree = open_tree.load(
        db, plan_id, recent_since=cutoff, include=() if detail_id is None else (detail_id,),
    )
    memo: dict = {}
    projected = open_rollup.rollup_open(db, tree, memo)
    targets = operator_projection._review_targets([projected], actor, db=db, open_nodes=True)
    states = operator_node_state.project(
        [projected], targets, db=db, now=_parse(clock), costs=costs, memo=memo,
        detail_ids=frozenset(() if detail_id is None else (detail_id,)),
        titles=open_tree.PlanTitles(db, plan_id, tree.rows),
    )
    owner = {"kind": plan_row["owner_kind"], "id": plan_row["owner_id"]}
    presented = operator_nodes.present_plan(projected, owner, targets, states, tree)
    nodes = presented["nodes"] if presented is not None else []
    durable = {str(row["id"]): (row["created_at"], row["size"]) for row in tree.rows}
    return shaped(plan_row, presented, nodes, durable, clock, tree.shape.parents)


def full_facts(db, plan_row, actor, clock: str, *, costs: bool = True) -> dict[str, Any]:
    memo: dict = {}
    projected = rollup.rollup_thread(str(plan_row["id"]), db=db, child_depth=0, memo=memo)
    targets = operator_projection._review_targets([projected], actor, db=db)
    states = operator_node_state.project(
        [projected], targets, db=db, now=_parse(clock), costs=costs, memo=memo,
    )
    owner = {"kind": plan_row["owner_kind"], "id": plan_row["owner_id"]}
    presented = operator_nodes.present_plan(projected, owner, targets, states)
    nodes = presented["nodes"] if presented is not None else []
    durable = {
        str(row["id"]): (row["created_at"], row["size"])
        for row in db.execute(
            "SELECT id,created_at,size FROM plan_nodes WHERE plan_id=?", (str(plan_row["id"]),)
        ).fetchall()
    }
    return shaped(plan_row, presented, nodes, durable, clock, node_states.parent_ids(nodes))


def shaped(plan_row, presented, nodes, durable, clock: str, parent_ids) -> dict[str, Any]:
    for node in nodes:
        created_at, size = durable.get(node["id"], (None, None))
        node["created_at"] = created_at
        node["size"] = size
    leaves = [node for node in nodes if node["id"] not in parent_ids]
    titles = {node["id"]: node["title"] for node in nodes}
    parents = {node["id"]: node["parent_id"] for node in nodes}
    return {
        "plan": presented["plan"] if presented is not None else dict(plan_row),
        "progress": presented["progress"] if presented is not None else {},
        "nodes": nodes,
        "leaves": leaves,
        "current_ids": presented["current_node_ids"] if presented is not None else [],
        "next_ids": presented["next_node_ids"] if presented is not None else [],
        # Agent-continuation facts, present only when no open leaf can move.
        "stuck": presented["display"].get("not_moving") if presented is not None else None,
        "clock": clock,
        "path": lambda node_id: _path(node_id, parents, titles),
    }


def _path(node_id: str, parents: dict, titles: dict) -> str | None:
    names: list[str] = []
    cursor = parents.get(node_id)
    while cursor:
        names.append(titles.get(cursor, ""))
        cursor = parents.get(cursor)
    return " / ".join(reversed(names)) or None
