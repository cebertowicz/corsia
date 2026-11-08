"""Regroup: re-hang one node under another branch, keeping everything it is (§4).

A plan drifts out of shape as it grows: work added under one outcome belongs
under another, and rows sit at top level because no branch existed when they
were written. Before this there was no way to say so -- a node's position was
fixed at creation, so tidying meant closing the row and adding it back, which
costs its history, its dependencies, its holds and waits, its cost record and
its identity. A regroup costs none of that: the node keeps its id, its events,
its ``commitment_id`` (the immutable original obligation that anchors cost
history), its dependencies, holds, waits and Later flag.

Two things make the write non-trivial. Nothing had to guard a node's ancestors
before, because nothing could re-hang one -- ``plans.hierarchy`` cycles are
plan-level -- so the cycle check lives here. And a node's stored subtree figures
come with it, so the branch it left and the branch it joined each have to be
re-derived; ``aggregates.apply`` measures a row's own change, which a move is
not, so ``plans.aggregates_move`` does that part.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import plans.aggregates_move as aggregates_move
import plans.intent_creation as intent_creation
import plans.node_creation as node_creation
import plans.node_states as node_states
import plans.service as service

#: The move's own event, with ``{"from": old parent, "to": new parent}``. The one
#: record of which branch a node left and joined, in the plan's own history.
EVENT = "node_reparented"

#: The new parent is not a node of this plan, or is no node at all: one answer
#: for both, so a regroup never says whether another plan's id exists.
NOT_IN_PLAN = "parent_id must be a node in this plan."
#: A node under itself is not a regroup; it is the cycle one level down, and it
#: would take its own subtree with it.
SELF_PARENT = "parent_id must name a different node: a node cannot be its own parent."
#: ``node_creation._validate_parent``'s own text, with the move that does work:
#: wanting a branch under a task is the common case, and the phase does not
#: exist yet.
UNDER_TASK = (
    "task nodes are leaves and cannot contain children: make a phase with "
    "add kind='phase', then move these under it"
)
#: The cause, the fact that makes it so, and the fix -- in the caller's own words
#: back at it, because a refused move usually means the ids were mixed up.
CYCLE = "{title} can't move inside its own branch ({parent} is beneath it)."

#: Returned by ``_inherited_intent`` when the node keeps the owner it has.
_KEEP = object()


@dataclass(frozen=True)
class Move:
    """One regroup: what to write, and the two levels its figures move between."""

    #: The columns of the one ``touch_node`` that performs the move.
    columns: dict[str, Any]
    #: The branch it left and the branch it joins, as written (None is top level).
    from_parent: str | None
    to_parent: str | None
    #: The level that held the figures and the one that will hold them: the same
    #: pair, except that a row stranded under an archived parent figures at plan
    #: level, which is every reader's root rule.
    from_level: str | None
    to_level: str | None


def plan(connection, node, parent_id) -> Move | None:
    """Return the write that re-hangs ``node``, or None when it is already there.

    Raises:
        PlanInputError: ``parent_id`` names this node, a node of another plan or
            no node, a task, or a descendant of the node being moved.
        PlanError: The new parent is retired or finished, the same refusals an
            ``add`` under it would give (``node_creation._validate_parent``).
    """
    old = node["parent_id"]
    if parent_id == old:
        return None
    if parent_id is not None:
        _refuse(connection, node, parent_id)
    return Move(
        columns={
            "parent_id": parent_id,
            # Last among its new siblings, by creation's own rule, so a moved row
            # is placed exactly where a row added there would have been.
            "order_key": node_creation._next_order(connection, node["plan_id"], parent_id),
            **_intent_columns(connection, node, parent_id),
        },
        from_parent=old,
        to_parent=parent_id,
        from_level=old if _holds(connection, old) else None,
        to_level=parent_id,
    )


def finish(connection, before, moved: Move | None, *, actor_id: str | None, provenance: str) -> None:
    """Re-derive both levels' figures and record the move, inside the write that made it."""
    if moved is None:
        return
    after = service._node_row(connection, before["id"])
    aggregates_move.move(
        connection, before, after, from_parent=moved.from_level, to_parent=moved.to_level,
        at=str(after["updated_at"]),
    )
    service._event(
        connection, before["id"], EVENT, actor_id=actor_id, provenance=provenance,
        payload={"from": moved.from_parent, "to": moved.to_parent},
    )


def _refuse(connection, node, parent_id: str) -> None:
    """Refuse every parent this node may not hang from, before anything is written."""
    if parent_id == node["id"]:
        raise service.PlanInputError(SELF_PARENT)
    parent = connection.execute("SELECT * FROM plan_nodes WHERE id=?", (parent_id,)).fetchone()
    if parent is None or parent["plan_id"] != node["plan_id"]:
        raise service.PlanInputError(NOT_IN_PLAN)
    if parent["kind"] == "task":
        raise service.PlanInputError(UNDER_TASK)
    # What the parent's own state forbids is creation's rule, so a regroup refuses
    # exactly what an `add` under the same node would; the task check above is
    # this module's only because the hint to make a branch is a regroup's own.
    node_creation._validate_parent(connection, node["plan_id"], parent_id)
    current: str | None = parent_id
    seen = set()
    while current is not None:
        if current == node["id"] or current in seen:
            raise service.PlanInputError(CYCLE.format(title=node["title"], parent=parent["title"]))
        seen.add(current)
        ancestor = connection.execute(
            "SELECT parent_id FROM plan_nodes WHERE id=?", (current,),
        ).fetchone()
        current = ancestor["parent_id"] if ancestor is not None else None


def _holds(connection, parent_id: str | None) -> bool:
    """Whether the level above a node is that parent, rather than the plan.

    A row under an archived parent is a root for every reader
    (``node_states.has_live_parent``), so its figures live on the plan, not on
    the dead branch it still names.
    """
    return parent_id is not None and connection.execute(
        f"SELECT 1 FROM plan_nodes WHERE id=?"
        f" AND {node_states.unarchived_sql('plan_nodes')}",
        (parent_id,),
    ).fetchone() is not None


def _intent_columns(connection, node, parent_id) -> dict[str, Any]:
    """Re-derive the inherited intent a moved node now serves, or leave it alone.

    Only a node that inherits an intent moves with its branch: a node that owns
    its intent owns it wherever it stands, which is ``mutations.edit_node``'s
    rule for a restated intent too. Top level is no branch, so a worker plan's
    tasks keep inheriting the step above the plan; a node with nothing to inherit
    and no intent of its own is left pointing where it did rather than stranded
    with neither, which no creation path can produce.
    """
    if node["intent_node_id"] is None:
        return {}
    found = (
        intent_creation.canonical(connection, parent_id)["id"]
        if parent_id is not None
        else intent_creation.inherited_node_id(connection, node["plan_id"])
    )
    if found is None and not (node["intent"] or "").strip():
        return {}
    return {"intent_node_id": found}
