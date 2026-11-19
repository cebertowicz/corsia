"""Compute a plan's stored aggregates from scratch, bottom-up (#731).

The same figures ``plans.aggregates.apply`` keeps incrementally: the 105 -> 106
migration backfill, and the helper a fixture uses after seeding raw rows. It
calls ``aggregates.refresh``, the hook's own per-node step, so the two cannot
classify differently; ``plans.aggregates_recount`` is what proves the result.

A row stranded under an archived parent is a root, as the readers and the recount
treat it, so an archived node never carries its stranded children's figures.

``folded_at`` of a row that already existed is an approximation: the later of the
node's last change and its folded children's ``folded_at``.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from plans import aggregates, node_states

_RESET = (
    "child_in_work", "own_in_work", "sub_open", "sub_done", "sub_failed", "sub_closed",
    "sub_in_work", "prop_open", "prop_done", "prop_failed", "prop_closed", "prop_in_work",
    "folded", "child_folded_done", "child_folded_failed", "child_folded_closed",
)


def _post_order(children: dict[str, list[str]], roots: list[str]) -> list[str]:
    """Node ids with every child before its parent, iteratively (plans can be deep)."""
    order: list[str] = []
    stack = list(roots)
    while stack:
        order.append(stack.pop())
        stack.extend(children[order[-1]])
    order.reverse()
    return order


def rebuild(connection: sqlite3.Connection, plan_id: str) -> None:
    """Set every aggregate of one plan's nodes, and the plan's top level, from its rows."""
    rows = {
        str(row["id"]): dict(row)
        for row in connection.execute("SELECT * FROM plan_nodes WHERE plan_id=?", (plan_id,))
    }
    live = {node_id for node_id, row in rows.items() if row["archived_at"] is None}
    children: dict[str, list[str]] = defaultdict(list)
    roots: list[str] = []
    for node_id, row in rows.items():
        # A row under an archived or absent parent is a root, as every reader treats it.
        (children[str(row["parent_id"])] if node_states.has_live_parent(row, live) else roots).append(node_id)
    for row in rows.values():
        row.update(dict.fromkeys(_RESET, 0))
        row["folded_at"] = row["fold_bucket"] = None
        row["listed"] = int(row["archived_at"] is None)
        row["own_last_changed"] = row["own_last_changed"] or row["updated_at"]
    for node_id in _post_order(children, roots):
        node = rows[node_id]
        kids = [rows[child] for child in children[node_id]]
        node["child_in_work"] = sum(kid["own_in_work"] for kid in kids)
        node["sub_open"] = sum(kid["prop_open"] for kid in kids)
        for name in ("done", "failed", "closed", "in_work"):
            node[f"sub_{name}"] = sum(kid[f"prop_{name}"] for kid in kids)
        for tone in aggregates.TONES:
            node[f"child_folded_{tone}"] = sum(kid["fold_bucket"] == tone for kid in kids)
        stamp = max([node["own_last_changed"], *(kid["folded_at"] or "" for kid in kids)])
        aggregates.refresh(node, stamp)
    aggregates.persist(connection, list(rows.values()))
    top = [rows[root] for root in roots]
    settled = ",".join(f"'{state}'" for state in aggregates.SETTLED_EVENT_STATES)
    connection.execute(
        "UPDATE plans SET top_open=?, top_done=?, top_failed=?, top_closed=?, top_in_work=?, "
        "top_roots=?, top_folded_done=?, top_folded_failed=?, top_folded_closed=?, "
        "settled_events=(SELECT COUNT(*) FROM plan_node_events e JOIN plan_nodes n ON n.id=e.node_id "
        f"WHERE n.plan_id=? AND e.new_state IN ({settled})) WHERE id=?",
        (
            sum(node["prop_open"] for node in top),
            *(sum(node[f"prop_{name}"] for node in top) for name in ("done", "failed", "closed", "in_work")),
            sum(node["parent_id"] is None and node["listed"] for node in top),
            *(sum(node["fold_bucket"] == tone for node in top) for tone in aggregates.TONES),
            plan_id, plan_id,
        ),
    )
