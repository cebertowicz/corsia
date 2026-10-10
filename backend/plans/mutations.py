"""Precise P2a plan mutations layered on the P1 transaction service."""

from __future__ import annotations


from plans import errors, service, writer_context
from plans.models import PlanNode

_UNSET = object()


def _assert_revision(node, expected_revision: int | None) -> None:
    """Reject stale writes while allowing operation-specific idempotent retries."""
    if expected_revision is not None:
        errors.require_node_revision(node, expected_revision)


def _node_for_plan(connection, plan_id: str, node_id: str):
    """Resolve a node only when it belongs to the requested concrete plan."""
    node = service._node_row(connection, node_id)
    if node["plan_id"] != plan_id:
        raise service.PlanError("plan node belongs to another plan")
    return node


def _live_siblings(connection, node) -> list:
    """Return the canonical ordered list of one parent's unarchived children.

    Every ordered read of a plan filters `archived_at IS NULL`, so this list is
    the only order a caller can observe, and a requested position is a position
    in it.
    """
    return connection.execute(
        "SELECT id, order_key FROM plan_nodes WHERE plan_id=? AND parent_id IS ? "
        "AND archived_at IS NULL ORDER BY order_key, id",
        (node["plan_id"], node["parent_id"]),
    ).fetchall()


def reorder_node(
    node_id: str,
    order_key: int,
    *,
    expected_revision: int | None = None,
    actor_id: str | None = None,
    provenance: str = "plan_mutation",
    db=None,
) -> PlanNode:
    """Move one node to a live sibling position with concurrency protection.

    ``order_key`` names a **position among unarchived siblings**, not a stored
    key. The two coincide until a sibling is archived; after that the stored
    keys are sparse, so the move is applied to the live list and the live keys
    are rewritten densely over that list. Archived siblings keep their historical
    keys, take part in no arithmetic, and never shift.
    """
    if isinstance(order_key, bool) or not isinstance(order_key, int) or order_key < 0:
        raise service.PlanInputError("order key must be non-negative")
    with service._connection(db) as connection, service._write(connection):
        node = service._live_node_row(connection, node_id)
        _assert_revision(node, expected_revision)
        siblings = _live_siblings(connection, node)
        current = [row["id"] for row in siblings]
        if order_key >= len(current):
            raise service.PlanError("order key is outside the sibling range")
        old_position = current.index(node_id)
        target = list(current)
        target.insert(order_key, target.pop(old_position))
        dense = [row["order_key"] for row in siblings] == list(range(len(siblings)))
        if target == current and dense:
            return service._node_from_row(connection, node)
        # Only the moved node changed; its siblings' order shifts leave their
        # revisions alone, so a reorder never invalidates a sibling's write.
        for position, sibling_id in enumerate(target):
            if sibling_id == node_id:
                service.touch_node(connection, node_id, order_key=position)
            else:
                writer_context.execute(
                    connection, "UPDATE plan_nodes SET order_key=? WHERE id=? AND order_key<>?",
                    (position, sibling_id, position),
                )
        service._event(
            connection, node_id, "node_reordered", actor_id=actor_id, provenance=provenance,
            payload={"from": old_position, "to": order_key},
        )
        return service._node_from_row(connection, service._node_row(connection, node_id))


def _next_edit_values(node, title, description, acceptance_criteria, watchdog_budget_seconds, size):
    """Normalize one edit and report whether requirements or size changed."""
    next_title = service.validate_node_title(title) if title is not None else node["title"]
    next_dod = (
        service._validate_dod(acceptance_criteria, bool(node["checkpoint_exception"]))
        if acceptance_criteria is not None else node["acceptance_criteria"]
    )
    next_description = description if description is not None else node["description"]
    next_watchdog = node["watchdog_budget_seconds"] if watchdog_budget_seconds is _UNSET else watchdog_budget_seconds
    next_size = node["size"] if size is _UNSET else size
    if next_watchdog is not None and (
        isinstance(next_watchdog, bool) or not isinstance(next_watchdog, int) or next_watchdog <= 0
    ):
        raise service.PlanInputError("watchdog budget seconds must be a positive integer")
    restated = (next_title, next_description, next_dod, next_watchdog) != (
        node["title"], node["description"], node["acceptance_criteria"], node["watchdog_budget_seconds"],
    )
    return next_title, next_description, next_dod, next_watchdog, next_size, restated


def edit_node(
    node_id: str,
    *,
    title: str | None = None,
    description: str | None = None,
    acceptance_criteria: str | None = None,
    watchdog_budget_seconds: int | None | object = _UNSET,
    difficulty_rerated: dict[str, str] | None = None,
    size: str | None | object = _UNSET,
    expected_revision: int | None = None,
    actor_id: str | None = None,
    provenance: str = "plan_mutation",
    db=None,
) -> PlanNode:
    """Amend durable node intent without replacing its stable identity."""
    if all(value is None for value in (title, description, acceptance_criteria, difficulty_rerated)) and watchdog_budget_seconds is _UNSET and size is _UNSET:
        raise service.PlanInputError("edit requires at least one changed field")
    if size is not _UNSET:
        from plans.size_fields import validate_size

        size = validate_size(size)
    with service._connection(db) as connection, service._write(connection):
        node = service._live_node_row(connection, node_id)
        _assert_revision(node, expected_revision)
        next_title, next_description, next_dod, next_watchdog, next_size, restated = _next_edit_values(
            node, title, description, acceptance_criteria, watchdog_budget_seconds, size,
        )
        size_changed = next_size != node["size"]
        if not restated and difficulty_rerated is None and not size_changed:
            return service._node_from_row(connection, node)
        requirements_changed = next_dod != node["acceptance_criteria"]
        if restated:
            service.touch_node(
                connection, node_id, title=next_title, description=next_description,
                acceptance_criteria=next_dod, watchdog_budget_seconds=next_watchdog,
                requirements_revision=node["requirements_revision"] + int(requirements_changed),
                **({"size": next_size} if size_changed else {}),
            )
        else:
            # Re-rating and size declaration preserve requirements and selection.
            service.touch_node(connection, node_id, **({"size": next_size} if size_changed else {}))
        if requirements_changed:
            from plans import admission

            admission.release(node_id, "requirements_changed", db=connection)
            admission.reselect_and_reconcile(node_id, actor_id=actor_id, provenance=provenance, db=connection)
        updated = service._node_row(connection, node_id)
        _record_edit_events(
            connection, node_id, restated, updated, next_title, next_dod,
            difficulty_rerated, node["size"], next_size, actor_id, provenance,
        )
        return service._node_from_row(connection, updated)


def _record_edit_events(
    connection, node_id, restated, updated, title, acceptance_criteria,
    difficulty_rerated, prior_size, next_size, actor_id, provenance,
) -> None:
    """Record independent restatement, re-rating, and size-declaration history."""
    if restated:
        service._event(
            connection, node_id, "node_restated", actor_id=actor_id, provenance=provenance,
            payload={
                "title": title,
                "acceptance_criteria": acceptance_criteria,
                "requirements_revision": updated["requirements_revision"],
            },
        )
    if difficulty_rerated is not None:
        service._event(
            connection, node_id, "difficulty_rerated", actor_id=actor_id,
            provenance=provenance, payload=difficulty_rerated,
        )
    if prior_size != next_size:
        service._event(
            connection, node_id, "size_declared", actor_id=actor_id,
            provenance=provenance, payload={"from": prior_size, "to": next_size},
        )


def insert_node(
    plan_id: str,
    *,
    kind: str,
    title: str,
    acceptance_criteria: str,
    before_node_id: str | None = None,
    parent_id: str | None = None,
    description: str | None = None,
    workflow_ref: str | None = None,
    workflow_version: str | None = None,
    workflow_step: str | None = None,
    difficulty: str | None = None,
    size: str | None = None,
    checkpoint_exception: bool = False,
    assignee: str | None = None,
    spawn_kind: str | None = None,
    watchdog_budget_seconds: int | None = None,
    actor_id: str | None = None,
    provenance: str = "plan_mutation",
    db=None,
) -> PlanNode:
    """Insert a DOD-bearing node before a sibling or append within a parent."""
    with service._connection(db) as connection, service._write(connection):
        order_key = None
        if before_node_id is not None:
            sibling = _node_for_plan(connection, plan_id, before_node_id)
            if parent_id is not None and sibling["parent_id"] != parent_id:
                raise service.PlanError("insert parent does not match target sibling")
            parent_id, order_key = sibling["parent_id"], sibling["order_key"]
        return service._create_node(
            connection, plan_id, kind=kind, title=title,
            acceptance_criteria=acceptance_criteria, parent_id=parent_id,
            order_key=order_key, description=description,
            workflow_ref=workflow_ref, workflow_version=workflow_version,
            workflow_step=workflow_step, difficulty=difficulty, size=size,
            checkpoint_exception=checkpoint_exception, assignee=assignee,
            spawn_kind=spawn_kind,
            watchdog_budget_seconds=watchdog_budget_seconds, follow_up_of=None,
            actor_id=actor_id, provenance=provenance, node_id=service._id(),
        )


def split_node(
    node_id: str,
    *,
    title: str,
    acceptance_criteria: str,
    description: str | None = None,
    expected_revision: int | None = None,
    actor_id: str | None = None,
    provenance: str = "plan_mutation",
    db=None,
) -> PlanNode:
    """Create a sibling task after an existing actionable task."""
    with service._connection(db) as connection, service._write(connection):
        source = service._live_node_row(connection, node_id)
        if source["kind"] != "task" or source["state"] in service._TERMINAL_STATES:
            raise service.PlanError("only non-terminal task nodes can be split")
        _assert_revision(source, expected_revision)
        sibling = service._create_node(
            connection, source["plan_id"], kind="task", title=title,
            acceptance_criteria=acceptance_criteria, parent_id=source["parent_id"],
            order_key=source["order_key"] + 1, description=description,
            workflow_ref=source["workflow_ref"],
            workflow_version=source["workflow_version"],
            workflow_step=source["workflow_step"], difficulty=source["difficulty"], size=None,
            checkpoint_exception=False, assignee=source["assignee"],
            spawn_kind=source["spawn_kind"],
            watchdog_budget_seconds=source["watchdog_budget_seconds"],
            follow_up_of=None, commitment_id=source["commitment_id"],
            actor_id=actor_id, provenance=provenance,
            node_id=service._id(),
        )
        service.touch_node(connection, node_id)
        service._event(
            connection, node_id, "node_split", actor_id=actor_id, provenance=provenance,
            payload={"split_node_id": sibling.id},
        )
        return sibling


def request_review(
    node_id: str,
    *,
    evidence: str | None = None,
    artifact_ids: object = None,
    expected_revision: int | None = None,
    actor_id: str | None = None,
    provenance: str = "plan_mutation",
    owner_thread_id: str | None = None,
    db=None,
) -> PlanNode:
    """Submit evidence and move active work to review through one event owner.

    ``owner_thread_id`` is the thread submitting, when one is: the results it
    owns are linked to the node as part of the submission.
    """
    from plans import review_evidence

    with service._connection(db) as connection, service._write(connection):
        return review_evidence.submit(
            node_id,
            evidence=evidence,
            artifact_ids=artifact_ids,
            expected_revision=expected_revision,
            actor_id=actor_id,
            provenance=provenance,
            owner_thread_id=owner_thread_id,
            db=connection,
        )


def fail_node(node_id: str, *, expected_revision: int | None = None,
              actor_id: str | None = None, provenance: str = "plan_mutation", db=None) -> PlanNode:
    """Fail a node once; a repeated failure observes the durable terminal state."""
    return _transition_idempotently(
        node_id, "failed", expected_revision=expected_revision,
        actor_id=actor_id, provenance=provenance, db=db,
    )


def _transition_idempotently(
    node_id: str,
    state: str,
    *,
    expected_revision: int | None,
    actor_id: str | None,
    provenance: str,
    db,
) -> PlanNode:
    """Apply a P1 transition after an optimistic-concurrency check."""
    with service._connection(db) as connection, service._write(connection):
        node = service._node_row(connection, node_id)
        _assert_revision(node, expected_revision)
        if node["state"] == state:
            return service._node_from_row(connection, node)
        return service._transition(
            connection, node_id, state, actor_id=actor_id, provenance=provenance,
            verification_result=None, verified_by=None,
        )


def attach_spawned_thread(*args, **kwargs) -> PlanNode:
    """Attach a direct child plan through the structured delegation service."""
    from plans.delegation import attach_spawned_thread as attach

    return attach(*args, **kwargs)



def replace_spawned_thread(*args, **kwargs):
    """Hand a delegated step to a replacement worker in one write (matrix F6).

    Returns ``delegation_replace.Replacement``: the node and every thread the
    replace cut off from the parent, for the caller to stop.
    """
    from plans.delegation_replace import replace_spawned_thread as replace

    return replace(*args, **kwargs)
