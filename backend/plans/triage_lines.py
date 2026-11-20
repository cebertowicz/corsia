"""Plan order is priority: a start that passes ready work above it is refused.

A named start (``activate``, ``start``, or a closing operation's
``next_node_id``) by the plan's owner is refused while a top-level item ranked
above it is ready: neither started nor waiting. The refusal names that item and
the two ways on: start it, or reorder this item above it so the plan says so.
A user's write, a conductor's write to another agent's plan and a delegating
parent's write are never refused here; the person can always override. An add
gets the top of the plan and a landing line instead (``plans.plan_top``).

Rank is a top-level node's position among the plan's open top-level nodes in
plan order. Ready is ``selection_validity``'s own answer (``slot_blockers``) for
each open leaf task, less a started task (one holding its slot, or delegated to
a worker) and a Later task (below the cut, so never passed over), so this
module holds no second readiness check: a top-level task is ready when it is,
and a top-level phase when a task under it is. Nothing here writes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import plans.admission_attempts as attempts
import plans.aggregates as aggregates
import plans.close_next_bounds as bounds
import plans.node_order as node_order
import plans.selection_validity as validity
import plans.service as service
from prompts import plan_start_order as order_text

#: Top-level states that take a rank: work still to settle or to review.
RANKED_STATES = frozenset({"pending", "active", "review_requested"})


@dataclass(frozen=True)
class Standing:
    """One plan's top-level order, and which top-level items are ready now."""

    open_roots: list[sqlite3.Row]
    ready: frozenset[str]
    root_of: dict[str, str] = field(default_factory=dict)

    def rank(self, root_id: str) -> int | None:
        """Return the 1-based rank among open top-level nodes, or None if unranked."""
        for index, row in enumerate(self.open_roots):
            if str(row["id"]) == root_id:
                return index + 1
        return None

    def ready_above(self, root_id: str) -> list[sqlite3.Row]:
        """Return the ready open top-level nodes ranked above ``root_id``, topmost first."""
        rank = self.rank(root_id)
        if rank is None:
            return []
        return [row for row in self.open_roots[: rank - 1] if str(row["id"]) in self.ready]


def position(db, plan_id: str, root_id: str) -> int:
    """Return a top-level node's position among the open top-level nodes.

    The position ``reorder``'s ``order_key`` names: how many open top-level
    siblings precede it in ``(order_key, id)`` order. One count over the open
    rows, so a caller asks only for the nodes it names.
    """
    return int(db.execute(
        "SELECT COUNT(*) FROM plan_nodes AS other, plan_nodes AS root "
        "WHERE root.id=? AND other.plan_id=? AND other.parent_id IS NULL "
        f"AND {aggregates.open_siblings_sql(db, 'other')} "
        "AND (other.order_key < root.order_key "
        "OR (other.order_key = root.order_key AND other.id < root.id))",
        (root_id, plan_id),
    ).fetchone()[0])


def standing(db, plan) -> Standing:
    """Read the plan's open top-level order and readiness from the open nodes only.

    Every input is open work: a ranked root is pending, active or in review,
    and a ready leaf is an open task. A node with only settled children is
    still a parent, which the open read answers per node
    (``node_order.open_nodes_with_paths``).
    """
    ordered = node_order.open_nodes_with_paths(db, str(plan["id"]))
    roots = [row for row, _path in ordered if row["parent_id"] is None]
    root_of = {
        str(row["id"]): path[0] if path else str(row["id"]) for row, path in ordered
    }
    leaves = [
        row for row, _path in ordered
        if not row["is_parent"] and not row["later"] and not row["spawned_thread_id"]
        and row["kind"] == "task" and row["state"] in node_order.OPEN_TASK_STATES
    ]
    blockers = validity.slot_blockers(db, [(row, plan) for row in leaves])
    eligible = [str(row["id"]) for row in leaves if blockers[str(row["id"])] is None]
    # A task already holding its slot is running, and a delegated one is
    # started: neither is waiting to be picked.
    running = attempts.open_attempts(eligible, db=db, compact=True)
    ready = frozenset(root_of[node_id] for node_id in eligible if node_id not in running)
    return Standing(
        [row for row in roots if row["state"] in RANKED_STATES],
        ready,
        root_of,
    )


def _owns(plan, actor_id: str | None) -> bool:
    """Whether ``actor_id`` is the thread whose own plan this is."""
    return (
        actor_id is not None
        and plan["owner_kind"] == "thread"
        and str(plan["owner_id"]) == actor_id
    )


def out_of_order(
    db, node, *, actor_id: str | None, compact: bool = False,
) -> str | None:
    """Return the refusal for a named start of ``node`` that passes ready work, or None.

    Call before the start writes, so the node is judged as it stands when
    chosen. None for a node that already holds its slot (a restart, or a task
    plan order already selected), for anything but a task, for a start by
    anyone but the plan's owner, and for a task that could not take its slot
    anyway: its own blocker is the answer the start already gives, and
    reordering would not change it. ``compact`` is the short form a ``next``
    block carries, no longer than its slot-held reason, within the block's cap.
    """
    if node["kind"] != "task":
        return None
    plan = service._plan_row(db, node["plan_id"])
    if not _owns(plan, actor_id) or attempts.open_attempt(db, str(node["id"])) is not None:
        return None
    where = standing(db, plan)
    root_id = where.root_of.get(str(node["id"]))
    above = [] if root_id is None else where.ready_above(root_id)
    # The node's own blocker is read only once ready work ranks above it, so a
    # start that passes nothing adds no read beyond ``standing``.
    if not above or validity.slot_blocker(db, node, plan) is not None:
        return None
    top = above[0]
    top_title = bounds.title(str(top["title"]))
    nested = str(root_id) != str(node["id"])
    root_kind = str(service._node_row(db, str(root_id))["kind"]) if nested else None
    if compact:
        return order_text.start_out_of_order_compact(top_title, root_kind=root_kind)
    return order_text.start_out_of_order(
        top_title, str(top["id"]), str(root_id),
        position(db, str(plan["id"]), str(top["id"])), root_kind=root_kind,
    )
