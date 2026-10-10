"""Unsettled work beneath one node, read from the stored subtree counts (#149 S2w).

A verdict or a close over a node asks whether anything beneath it is still unsettled:
pending, active or in review. The node's own ``sub_open`` answers for every such row
the pane lists as open, and a pending parent whose children have all settled (a stale
container, which the fold hides) is found through its own small index. Neither read
touches the settled history under the node, so a verdict over a long finished branch
costs what its open work costs. A database below the aggregates schema (an older rung
of the migration ladder) has no counts, and its callers walk the subtree as they always did.
"""

from __future__ import annotations

import sqlite3

from plans import aggregates, node_states


def counted(connection: sqlite3.Connection, node_id: str) -> tuple[int, int] | None:
    """Return ``(sub_open, sub_failed)`` of one node, or None below the aggregates schema."""
    if not aggregates.ready(connection):
        return None
    row = connection.execute(
        "SELECT sub_open, sub_failed FROM plan_nodes WHERE id=?", (node_id,),
    ).fetchone()
    return None if row is None else (int(row[0]), int(row[1]))


def named(connection: sqlite3.Connection, node_id: str, limit: int, *, open_below: bool) -> list[str]:
    """Return up to ``limit`` unsettled descendants of ``node_id`` in sibling order.

    Unsettled is not terminal, reached only through nodes still in the work: beneath a
    retired node nothing counts. ``open_below`` is the node's ``sub_open`` being above
    zero; when it is not, only the plan's stale containers can be unsettled beneath it.
    """
    plan = connection.execute(
        "SELECT plan_id FROM plan_nodes WHERE id=?", (node_id,),
    ).fetchone()
    if plan is None:
        return []
    rows = {
        str(row["id"]): row for row in connection.execute(
            "SELECT id,parent_id,state,archived_at,order_key,created_at FROM plan_nodes"
            " WHERE plan_id=? AND archived_at IS NULL AND folded=1 AND state='pending'",
            (plan[0],),
        )
    }
    if open_below:
        rows.update({
            str(row["id"]): row for row in connection.execute(
                "SELECT id,parent_id,state,archived_at,order_key,created_at FROM plan_nodes"
                f" WHERE plan_id=? AND {node_states.listed_open_sql('plan_nodes')}",
                (plan[0],),
            )
        })
    above: dict[str, sqlite3.Row | None] = {}

    def lookup(parent_id: str):
        if parent_id not in above:
            above[parent_id] = connection.execute(
                "SELECT parent_id,state,archived_at FROM plan_nodes WHERE id=?", (parent_id,),
            ).fetchone()
        return above[parent_id]

    found = [
        row for row in rows.values()
        if row["state"] not in node_states._TERMINAL_STATES and _beneath(lookup, row, node_id)
    ]
    found.sort(key=lambda row: (row["order_key"], row["created_at"]))
    return [str(row["id"]) for row in found[:limit]]


def _beneath(lookup, row, node_id: str) -> bool:
    """Whether ``row`` is under ``node_id`` by a chain of nodes still in the work."""
    parent = row["parent_id"]
    while parent is not None:
        if parent == node_id:
            return True
        ancestor = lookup(str(parent))
        if ancestor is None or node_states.retired(ancestor):
            return False
        parent = ancestor["parent_id"]
    return False
