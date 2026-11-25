"""Atomic structured-plan follow-up creation and acceptance."""

from __future__ import annotations

import plans.aggregates as aggregates
import plans.errors as errors
import plans.service as service
from plans import workflow_fields


def _canonical_intent(connection, node_id: str) -> str:
    from plans.intent_creation import canonical

    return canonical(connection, node_id)["id"]


def _unsettled_parent(connection, parent_id: str | None) -> str | None:
    """Where a follow-up lands: the source's parent, else its nearest unsettled ancestor.

    A settled parent takes no new child, so the follow-up goes up to the first
    ancestor that is neither settled nor archived, or to the top level.
    """
    while parent_id is not None:
        parent = service._node_row(connection, parent_id)
        if parent["state"] not in service._TERMINAL_STATES and parent["archived_at"] is None:
            return parent_id
        parent_id = parent["parent_id"]
    return None


def _source_spot(connection, source, parent_id: str | None) -> int | None:
    """The follow-up's place among ``parent_id``'s open children: where its source sits.

    Plan order is priority, so a redo, or what an accepted step left over, keeps
    its source's place instead of dropping to the bottom (and from there into
    Later past the cap). A settled source has left the open order, so the
    follow-up takes its place; an open one keeps it, with the follow-up right
    after. A follow-up that lands higher up the tree sits where its source's
    branch sits. None (last) when the source is not under ``parent_id``.
    """
    anchor = source
    while anchor["parent_id"] != parent_id:
        if anchor["parent_id"] is None:
            return None
        anchor = service._node_row(connection, anchor["parent_id"])
    mark = (anchor["order_key"], str(anchor["id"]))
    rows = connection.execute(
        "SELECT id, order_key FROM plan_nodes WHERE plan_id=? AND parent_id IS ? "
        f"AND {aggregates.open_siblings_sql(connection)}",
        (source["plan_id"], parent_id),
    ).fetchall()
    return sum(1 for row in rows if (row["order_key"], str(row["id"])) <= mark)


def add_follow_up(
    node_id: str,
    *,
    title: str,
    acceptance_criteria: str,
    size: str | None = None,
    actor_id: str | None = None,
    provenance: str = "plan_service",
    expected_revision: int | None = None,
    inherit_commitment: bool = False,
    description: str | None = None,
    spawn_kind: str | None = None,
    intent: str | None = None,
    step: workflow_fields.FollowUpStep = "none",
    db=None,
):
    """Create one DOD-bearing task linked to a follow-up chain.

    Reached by ``accept_with_follow_up``, which has already settled the
    source and, with ``step="identity"``, counts as the workflow step the
    source was; as a standalone non-blocking sidetrack that leaves the source
    exactly where it was; and by a rejection, whose revision carries the
    source's description and delegation kind and, with ``step="rules"``, stays
    the workflow step the source was under its rules. Only the standalone boundary
    path supplies ``expected_revision``, because only it owes concurrency
    evidence. The chain has no depth limit, and every follow-up keeps its
    ``follow_up_of`` link, so an initiative's lineage stays whole (C2).
    """
    with service._connection(db) as connection, service._write(connection):
        source = service._node_row(connection, node_id)
        service.require_live_node(source)
        if expected_revision is not None:
            errors.require_node_revision(source, expected_revision)
        follow_up_id = service._id()
        step_fields = workflow_fields.follow_up_step_fields(connection, source, step)
        parent_id = _unsettled_parent(connection, source["parent_id"])
        follow_up = service._create_node(
            connection, source["plan_id"], kind="task", title=title,
            acceptance_criteria=acceptance_criteria,
            parent_id=parent_id, order_key=_source_spot(connection, source, parent_id),
            description=description,
            size=size, intent=intent, **step_fields,
            intent_node_id=(None if intent is not None else _canonical_intent(connection, source["id"])),
            checkpoint_exception=False, assignee=source["assignee"],
            spawn_kind=spawn_kind,
            watchdog_budget_seconds=source["watchdog_budget_seconds"],
            follow_up_of=node_id,
            commitment_id=(
                source["commitment_id"] if inherit_commitment else follow_up_id
            ),
            actor_id=actor_id, provenance=provenance, node_id=follow_up_id,
        )
        return follow_up


def accept_with_follow_up(
    node_id: str,
    *,
    title: str,
    acceptance_criteria: str,
    size: str | None = None,
    intent: str | None = None,
    actor_id: str | None = None,
    provenance: str = "plan_service",
    verification_result: str | None = None,
    verified_by: str | None = None,
    expected_revision: int | None = None,
    db=None,
):
    """Accept a reviewed node and create its linked follow-up atomically."""
    with service._connection(db) as connection, service._write(connection):
        node = service._node_row(connection, node_id)
        if expected_revision is not None:
            errors.require_node_revision(node, expected_revision)
        if node["state"] != "review_requested":
            raise service.PlanError(
                "only review-requested nodes can accept with a follow-up"
            )
        service._transition(
            connection, node_id, "accepted_with_followup", actor_id=actor_id,
            provenance=provenance, verification_result=verification_result,
            verified_by=verified_by,
        )
        follow_up = add_follow_up(
            node_id, title=title, acceptance_criteria=acceptance_criteria, size=size,
            intent=intent, actor_id=actor_id, provenance=provenance,
            inherit_commitment=True, step="identity", db=connection,
        )
        return service._node_from_row(
            connection, service._node_row(connection, node_id)
        ), follow_up
