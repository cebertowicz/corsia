"""Atomic structured-plan follow-up creation and acceptance."""

from __future__ import annotations

import plans.errors as errors
import plans.service as service


def _canonical_intent(connection, node_id: str) -> str:
    from plans.intent_words import canonical

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
    intent_words: list[dict[str, str]] | None = None,
    same_step: bool = False,
    db=None,
):
    """Create one DOD-bearing task linked to a follow-up chain.

    Reached by ``accept_with_follow_up``, which has already settled the
    source; as a standalone non-blocking sidetrack that leaves the source
    exactly where it was; and by a rejection, whose revision carries the
    source's description and delegation kind and, with ``same_step``, stays
    the workflow step the source was. Only the standalone boundary
    path supplies ``expected_revision``, because only it owes concurrency
    evidence. The chain has no depth limit, and every follow-up keeps its
    ``follow_up_of`` link, so an initiative's lineage stays whole (C2).
    """
    with service._connection(db) as connection, service._write(connection):
        source = service._node_row(connection, node_id)
        service.require_live_node(source)
        if expected_revision is not None:
            errors.require_node_revision(source, expected_revision)
        from plans import workflow_fields

        follow_up_id = service._id()
        step = workflow_fields.same_step_fields(connection, source) if same_step else workflow_fields.NOT_A_STEP
        follow_up = service._create_node(
            connection, source["plan_id"], kind="task", title=title,
            acceptance_criteria=acceptance_criteria,
            parent_id=_unsettled_parent(connection, source["parent_id"]),
            order_key=None, description=description,
            size=size, intent=intent, **step,
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
        if intent_words:
            from plans import intent_words as words
            words.append(connection, follow_up.id, intent_words, revision=follow_up.requirements_revision)
        return follow_up


def accept_with_follow_up(
    node_id: str,
    *,
    title: str,
    acceptance_criteria: str,
    size: str | None = None,
    intent: str | None = None,
    intent_words: list[dict[str, str]] | None = None,
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
            intent=intent, intent_words=intent_words, actor_id=actor_id, provenance=provenance,
            inherit_commitment=True, db=connection,
        )
        return service._node_from_row(
            connection, service._node_row(connection, node_id)
        ), follow_up
