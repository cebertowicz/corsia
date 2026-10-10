"""The two triage lines an agent reads when plan order and its own choice disagree.

``rank`` rides an ``add`` that lands below ready top-level work, and ``jumped``
rides a named start that passes ready top-level work. Both are guidance in a
tool result, never a refusal, and both are for the plan's own owner: a user's
write, a conductor's append to another agent's plan and a delegating parent's
write carry neither.

Rank is a top-level node's position among the plan's open top-level nodes in
plan order. Ready is ``selection_validity``'s own answer (``slot_blockers``) for
each open leaf task, less a task that already holds its slot (it is running,
not waiting to be picked), so this module holds no second readiness check: a
top-level task is ready when it is, and a top-level phase when a task under it
is. Nothing here writes; the one durable fact, ``JUMPED_READY_NODE_ID``, is
handed to the start's existing transition event by its caller.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import plans.admission_attempts as attempts
import plans.node_order as node_order
import plans.node_states as node_states
import plans.selection_validity as validity
import plans.service as service
from prompts import plan_tools as text

#: The start event's payload key naming the ready item a start jumped (T3).
JUMPED_READY_NODE_ID = "jumped_ready_node_id"

#: Top-level states that take a rank: work still to settle or to review.
RANKED_STATES = frozenset({"pending", "active", "review_requested"})

#: Ready items the ``rank`` line names.
READY_ABOVE_LIMIT = 2


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


@dataclass(frozen=True)
class Jump:
    """A named start that passed ready work: the line, and the item it passed."""

    line: str
    ready_node_id: str

    @property
    def payload(self) -> dict[str, str]:
        """The fact the start's transition event records."""
        return {JUMPED_READY_NODE_ID: self.ready_node_id}


def position(db, plan_id: str, root_id: str) -> int:
    """Return a top-level node's position among all unarchived top-level nodes.

    The position ``reorder``'s ``order_key`` names: how many unarchived
    top-level siblings precede it in ``(order_key, id)`` order. One count, so a
    caller asks only for the nodes it names.
    """
    return int(db.execute(
        "SELECT COUNT(*) FROM plan_nodes AS other, plan_nodes AS root "
        "WHERE root.id=? AND other.plan_id=? AND other.parent_id IS NULL "
        f"AND {node_states.unarchived_sql('other')} "
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
        if not row["has_child_in_work"]
        and row["kind"] == "task" and row["state"] in node_order.OPEN_TASK_STATES
    ]
    blockers = validity.slot_blockers(db, [(row, plan) for row in leaves])
    eligible = [str(row["id"]) for row in leaves if blockers[str(row["id"])] is None]
    # A task already holding its slot is running, not waiting to be picked.
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


def add_line(db, plan, node_id: str, *, actor_id: str | None) -> str | None:
    """Return the ``rank`` line for a task just added to its owner's plan, or None.

    A line only when the new node is top-level and ready top-level work ranks
    above it. ``insert`` never calls this: its caller chose the position.
    """
    node = service._node_row(db, node_id)
    if node["parent_id"] is not None or not _owns(plan, actor_id):
        return None
    where = standing(db, plan)
    above = where.ready_above(node_id)
    rank = where.rank(node_id)
    if not above or rank is None:
        return None
    return text.triage_rank(
        rank, len(where.open_roots),
        [(str(row["title"]), str(row["id"])) for row in above[:READY_ABOVE_LIMIT]],
        position(db, str(plan["id"]), str(above[0]["id"])),
    )


def jump_for_start(db, node, *, actor_id: str | None) -> Jump | None:
    """Return what a named start of ``node`` passes over, or None.

    Call before the start writes, so the node is judged as it stands when
    chosen. None for a node that already holds its slot (a restart, or a task
    plan order already selected), for anything but a task, and for a start by
    anyone but the plan's owner.
    """
    if node["kind"] != "task":
        return None
    plan = service._plan_row(db, node["plan_id"])
    if not _owns(plan, actor_id) or attempts.open_attempt(db, str(node["id"])) is not None:
        return None
    where = standing(db, plan)
    root_id = where.root_of.get(str(node["id"]))
    above = [] if root_id is None else where.ready_above(root_id)
    rank = where.rank(str(above[0]["id"])) if above else None
    if root_id is None or rank is None:
        return None
    top = above[0]
    line = text.triage_jumped(
        str(top["title"]), str(top["id"]), rank, len(where.open_roots),
        str(root_id), position(db, str(plan["id"]), str(top["id"])),
    )
    return Jump(line, str(top["id"]))
