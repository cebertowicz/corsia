"""Regroup: re-hang one node under another branch, keeping everything it is (§4).

A plan drifts out of shape as it grows: work added under one outcome belongs
under another, and rows sit at top level because no branch existed when they
were written. A regroup moves the row without closing and re-adding it: the
node keeps its id, its events,
its ``commitment_id`` (the immutable original obligation that anchors cost
history), its dependencies, holds, waits and Later flag. It keeps the intent it
serves too: ``intent_node_id`` names the node a row was made from (a follow-up's
source, a worker plan's step above), never the branch it hangs under, so where
the row hangs has no bearing on it -- exactly as a row added at that position
would not inherit the branch's intent either.

Three things make the write non-trivial. This is the only writer of a node's
``parent_id`` (``plans.hierarchy`` cycles are plan-level), so the node-cycle
check lives here. A node's stored subtree figures
come with it, so the branch it left and the branch it joined each have to be
re-derived; ``aggregates.apply`` measures a row's own change, which a move is
not, so ``plans.aggregates_move`` does that part. And ``parent_id`` is part of
the wait graph (a parent finishes after its children, which inherit its
edges), so ``plans.dependencies``, the graph's one guard, refuses a move that
would leave work waiting on itself, and the plan is reconciled after it: work
the move put behind an unmet prerequisite gives up its slot, and work it freed
is selected. A move changes only inherited edges, so it raises no
``blocked_raised``/``blocked_cleared``: those are own-edge deltas
(``node_waits.waits_on_dependency``), which ``add_dependency`` on a branch does
not write for the work beneath it either.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import plans.admission as admission
import plans.aggregates_move as aggregates_move
import plans.dependencies as dependencies
import plans.node_creation as node_creation
import plans.node_states as node_states
import plans.run_end as run_end
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
#: The wait-graph form of the same mistake: the branch already waits, directly or
#: through other work, on something that would then wait on it.
WAIT_CYCLE = (
    "{title} can't move under {parent}: it already waits, directly or through other "
    "work, on something that would then wait on it. Remove that dependency first, "
    "or move it somewhere else."
)
#: A run's steps come from its definition and its end is read from them by
#: ``parent_id`` (``plans.run_end``), so a step moved away would end the run without
#: it -- the skip ``workflow_fields.refuse_required_skip`` refuses for a close.
RUN_STEP = (
    "{title} is a step of a workflow run, so it stays in its run: finish it there, "
    "or close it with a reason if the run lets it be skipped."
)

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
        PlanInputError: ``node`` is a workflow run's step, or ``parent_id`` names
            this node, a node of another plan or no node, a task, or a descendant
            of the node being moved.
        PlanError: The new parent is retired or finished, the same refusals an
            ``add`` under it would give (``node_creation._validate_parent``).
    """
    old = node["parent_id"]
    if parent_id == old:
        return None
    if node["workflow_step"] is not None:
        raise service.PlanInputError(RUN_STEP.format(title=node["title"]))
    if parent_id is not None:
        _refuse(connection, node, parent_id)
    return Move(
        columns={
            "parent_id": parent_id,
            # Last among its new siblings, by creation's own rule, so a moved row
            # is placed exactly where a row added there would have been.
            "order_key": node_creation._next_order(connection, node["plan_id"], parent_id),
        },
        from_parent=old,
        to_parent=parent_id,
        from_level=old if _holds(connection, old) else None,
        to_level=parent_id,
    )


def finish(connection, before, moved: Move | None, *, actor_id: str | None, provenance: str) -> None:
    """Re-derive both levels' figures and record the move, inside the write that made it.

    Raises:
        PlanInputError: The node, where it now hangs, would wait on itself. The
            write's own savepoint takes the move back.
    """
    if moved is None:
        return
    # Top level is under nothing, so it adds no wait link to judge.
    if moved.to_parent is not None and dependencies.placement_would_cycle(connection, before["id"]):
        parent = service._node_row(connection, moved.to_parent)
        raise service.PlanInputError(WAIT_CYCLE.format(title=before["title"], parent=parent["title"]))
    after = service._node_row(connection, before["id"])
    aggregates_move.move(
        connection, before, after, from_parent=moved.from_level, to_parent=moved.to_level,
        at=str(after["updated_at"]),
    )
    service._event(
        connection, before["id"], EVENT, actor_id=actor_id, provenance=provenance,
        payload={"from": moved.from_parent, "to": moved.to_parent},
    )
    # The prerequisites holding the moved branch changed, as an edge change does, so
    # the plan is reconciled -- by the caller, once, after its own releases
    # (``mutations.edit_node``, ``bulk_nodes.edit_many``), never here mid-write.
    # Work moved out of a run may have been the run's last open work: it ends here,
    # as it would have when that work settled. Work moved in is what an `add` there
    # would be, which keeps the run open until it settles.
    run_end.record_from(connection, moved.from_parent)


def reconcile(connection, node, *, actor_id: str | None, provenance: str) -> None:
    """Reconcile the plan a move changed, once, after the edit's own releases.

    The prerequisites holding the moved branch changed, as an edge change does, so
    this is the reconcile ``add_dependency`` runs. ``mutations.edit_node`` calls it
    last, and not when a requirements change already reconciled through
    ``reselect_and_reconcile`` after giving the task its slot back; a bulk regroup
    defers it and reconciles once for its whole list (``bulk_nodes.edit_many``).
    """
    admission.reconcile_plan(
        str(node["plan_id"]), actor_id=actor_id, provenance=provenance,
        causing_executor=actor_id, db=connection,
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
    # The stored chain, archived rows included, not the live chain readers use
    # (``node_states.live_chain`` stops at an archived row, so it would miss the
    # node above a stranded one); the seen set ends the walk on any loop.
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
