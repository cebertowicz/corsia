"""Validated creation of one structured-plan node."""

from __future__ import annotations

import sqlite3

import plans.node_states as node_states
import plans.service as service
from plans import aggregates, writer_context
from plans.models import PlanNode


def _validate_parent(
    connection: sqlite3.Connection,
    plan_id: str,
    parent_id: str | None,
) -> None:
    if parent_id is None:
        return
    parent = service._node_row(connection, parent_id)
    if parent["plan_id"] != plan_id:
        raise service.PlanError("node parent belongs to another plan")
    if node_states.retired(parent):
        # Nothing may be added under a node out of the work (#1887).
        state = "archived" if parent["archived_at"] is not None else "closed"
        raise service.PlanError(f"node parent is {state}")
    if parent["state"] in service._TERMINAL_STATES:
        # A finished parent over open work is impossible.
        shown = "accepted" if parent["state"] in service._ACCEPTED_STATES else parent["state"]
        raise service.PlanError(
            f"node parent is {shown}: finished work takes no new child. Add the work at the "
            "top level or under a node that is still open; to continue finished work, "
            "use add_follow_up on it"
        )
    if parent["kind"] == "task":
        raise service.PlanInputError("task nodes are leaves and cannot contain children")


def _next_order(
    connection: sqlite3.Connection,
    plan_id: str,
    parent_id: str | None,
) -> int:
    """The key after every unarchived sibling: a new node goes last, settled siblings included.

    One backward step of ``idx_plan_nodes_tree`` from the end, so it costs nothing in the
    history; the settled siblings keep the keys they were given, and the new node must
    not tie with one of them.
    """
    row = connection.execute(
        "SELECT COALESCE(MAX(order_key) + 1, 0) AS order_key FROM plan_nodes "
        "WHERE plan_id=? AND parent_id IS ? AND archived_at IS NULL",
        (plan_id, parent_id),
    ).fetchone()
    return int(row["order_key"])


def _commitment(
    connection: sqlite3.Connection,
    plan_id: str,
    node_id: str,
    parent_id: str | None,
    commitment_id: str | None,
) -> str:
    from plans import commitments

    return commitments.resolve_for_creation(
        plan_id,
        node_id,
        parent_id=parent_id,
        explicit_commitment_id=commitment_id,
        db=connection,
    )


def _validate_node_input(
    connection: sqlite3.Connection,
    plan_id: str,
    *,
    kind: str,
    assignee: str | None,
    watchdog_budget_seconds: int | None,
    parent_id: str | None,
    follow_up_of: str | None,
) -> None:
    """Reject every invalid field before the first write.

    Raises:
        PlanInputError: A caller-fixable field is invalid.
        PlanError: The follow-up source belongs to another plan.
    """
    service._plan_row(connection, plan_id)
    if kind not in service.NODE_KINDS:
        raise service.PlanInputError(f"invalid plan node kind: {kind}")
    if assignee is not None and assignee not in service.ASSIGNEES:
        raise service.PlanInputError(f"invalid plan node assignee: {assignee}")
    if watchdog_budget_seconds is not None and (
        isinstance(watchdog_budget_seconds, bool)
        or not isinstance(watchdog_budget_seconds, int)
        or watchdog_budget_seconds <= 0
    ):
        raise service.PlanInputError(
            "watchdog budget seconds must be a positive integer"
        )
    _validate_parent(connection, plan_id, parent_id)
    if (
        follow_up_of is not None
        and service._node_row(connection, follow_up_of)["plan_id"] != plan_id
    ):
        raise service.PlanError("follow-up source belongs to another plan")


def _open_order_slot(
    connection: sqlite3.Connection,
    plan_id: str,
    parent_id: str | None,
    order_key: int | None,
) -> int:
    """Resolve the new node's position, shifting later open siblings down for it.

    ``order_key`` names a position among the open siblings, as ``reorder`` does: the
    new node takes the stored key of the open sibling it displaces, and that sibling
    and every later open one shift down. Settled siblings keep their keys, because
    only the order among open rows is ever read.

    Returns:
        The order key the new node should occupy.

    Raises:
        PlanInputError: The requested order key is negative.
    """
    if order_key is not None and order_key < 0:
        raise service.PlanInputError("order key must be non-negative")
    open_siblings = aggregates.open_siblings_sql(connection)
    key = None
    if order_key is not None:
        displaced = connection.execute(
            "SELECT order_key FROM plan_nodes WHERE plan_id=? AND parent_id IS ? "
            f"AND {open_siblings} ORDER BY order_key,id LIMIT 1 OFFSET ?",
            (plan_id, parent_id, order_key),
        ).fetchone()
        key = None if displaced is None else int(displaced["order_key"])
    if key is None:
        key = _next_order(connection, plan_id, parent_id)
    writer_context.execute(
        # An insert shifts its siblings' order, which is not their change:
        # their revisions stay put (see service.touch_node).
        connection, "UPDATE plan_nodes SET order_key=order_key+1 WHERE plan_id=? AND parent_id IS ? "
        f"AND order_key>=? AND {open_siblings}",
        (plan_id, parent_id, key),
    )
    return key


def _insert_node_row(
    connection,
    base_values,
    workflow_ref,
    workflow_version,
    workflow_step,
    difficulty,
    size,
    intent,
    intent_node_id,
) -> None:
    """Insert against the current node shape or its pre-v51 migration predecessor.

    The one creation write of a node, so it also runs the aggregate hook: the new
    row joins its parent's counts and every ancestor's, in the same transaction.
    """
    workflow_columns = {"workflow_ref", "workflow_version", "workflow_step", "difficulty"}
    present_columns = {row[1] for row in connection.execute("PRAGMA table_info(plan_nodes)")}
    if workflow_columns <= present_columns:
        if {"intent", "intent_node_id"} <= present_columns:
            writer_context.execute(
                connection, "INSERT INTO plan_nodes "
                "(id,plan_id,parent_id,order_key,kind,title,description,intent,intent_node_id,workflow_ref,"
                "workflow_version,workflow_step,difficulty,size,acceptance_criteria,"
                "checkpoint_exception,assignee,spawn_kind,watchdog_budget_seconds,"
                "follow_up_of,requirements_revision,commitment_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    *base_values[:7], intent, intent_node_id, workflow_ref, workflow_version, workflow_step,
                    difficulty, size, *base_values[7:],
                ),
            )
        else:
            writer_context.execute(
                connection, "INSERT INTO plan_nodes "
                "(id,plan_id,parent_id,order_key,kind,title,description,workflow_ref,"
                "workflow_version,workflow_step,difficulty,size,acceptance_criteria,"
                "checkpoint_exception,assignee,spawn_kind,watchdog_budget_seconds,"
                "follow_up_of,requirements_revision,commitment_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    *base_values[:7], workflow_ref, workflow_version, workflow_step, difficulty, size,
                    *base_values[7:],
                ),
            )
    else:
        if any(value is not None for value in (workflow_ref, workflow_version, workflow_step, difficulty)):
            raise service.PlanInputError("workflow fields require schema version 51")
        if size is not None:
            raise service.PlanInputError("size requires schema version 91")
        # Mid-ladder migrations use the current plan service before v51 runs.
        writer_context.execute(
            connection, "INSERT INTO plan_nodes "
            "(id,plan_id,parent_id,order_key,kind,title,description,"
            "acceptance_criteria,checkpoint_exception,assignee,spawn_kind,"
            "watchdog_budget_seconds,follow_up_of,requirements_revision,"
            "commitment_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            base_values,
        )
    aggregates.apply(connection, base_values[0], at=base_values[-1])


def _creation_values(
    connection, plan_id, node_id, commitment_id, *, kind, title, acceptance_criteria,
    parent_id, order_key, description, workflow_ref, workflow_version, workflow_step,
    difficulty, size, intent, intent_node_id, checkpoint_exception, assignee, spawn_kind,
    watchdog_budget_seconds, follow_up_of,
):
    """Validate one node and prepare its durable values before insertion."""
    _validate_node_input(
        connection, plan_id, kind=kind, assignee=assignee,
        watchdog_budget_seconds=watchdog_budget_seconds, parent_id=parent_id, follow_up_of=follow_up_of,
    )
    from core.size_rating import UNKNOWN
    from plans.workflow_fields import creation_values
    from plans.size_fields import validate_size

    workflow_ref, workflow_version, workflow_step, difficulty = creation_values(
        workflow_ref=workflow_ref, workflow_version=workflow_version,
        workflow_step=workflow_step, difficulty=difficulty,
    )
    size = validate_size(size)
    if kind == "task" and size is None:
        size = UNKNOWN
    from plans import intent_words

    if intent is not None:
        intent = intent_words.validate_intent(intent)
    elif intent_node_id is None:
        intent_node_id = intent_words.inherited_node_id(connection, plan_id)
    order_key = _open_order_slot(connection, plan_id, parent_id, order_key)
    resolved_commitment = _commitment(connection, plan_id, node_id, parent_id, commitment_id)
    now = service._now()
    base_values = (
        node_id, plan_id, parent_id, order_key, kind, service.validate_node_title(title), description,
        service._validate_dod(acceptance_criteria, checkpoint_exception), int(checkpoint_exception),
        assignee, spawn_kind, watchdog_budget_seconds, follow_up_of, 1, resolved_commitment, now, now,
    )
    return (
        base_values, workflow_ref, workflow_version, workflow_step, difficulty, size,
        intent, intent_node_id, resolved_commitment,
    )


def create_node(
    connection: sqlite3.Connection,
    plan_id: str,
    *,
    kind: str,
    title: str,
    acceptance_criteria: str,
    parent_id: str | None,
    order_key: int | None,
    description: str | None,
    workflow_ref: str | None,
    workflow_version: str | None,
    workflow_step: str | None,
    difficulty: str | None,
    size: str | None,
    checkpoint_exception: bool,
    assignee: str | None,
    spawn_kind: str | None,
    watchdog_budget_seconds: int | None,
    follow_up_of: str | None,
    actor_id: str | None,
    provenance: str,
    node_id: str,
    intent: str | None = None,
    intent_node_id: str | None = None,
    commitment_id: str | None = None,
) -> PlanNode:
    """Validate and insert a node without opening another transaction."""
    (
        base_values, workflow_ref, workflow_version, workflow_step, difficulty, size,
        intent, intent_node_id, commitment_id,
    ) = _creation_values(
        connection, plan_id, node_id, commitment_id, kind=kind, title=title,
        acceptance_criteria=acceptance_criteria, parent_id=parent_id, order_key=order_key,
        description=description, workflow_ref=workflow_ref, workflow_version=workflow_version,
        workflow_step=workflow_step, difficulty=difficulty, size=size, intent=intent,
        intent_node_id=intent_node_id, checkpoint_exception=checkpoint_exception, assignee=assignee,
        spawn_kind=spawn_kind,
        watchdog_budget_seconds=watchdog_budget_seconds, follow_up_of=follow_up_of,
    )
    _insert_node_row(connection, base_values, workflow_ref, workflow_version, workflow_step, difficulty, size,
                     intent, intent_node_id)
    service._event(
        connection, node_id, "node_created", actor_id=actor_id, provenance=provenance,
        payload={
            "checkpoint_exception": checkpoint_exception, "workflow_ref": workflow_ref,
            "workflow_version": workflow_version, "workflow_step": workflow_step,
            "difficulty": difficulty, "size": size, "follow_up_of": follow_up_of,
            "commitment_id": commitment_id,
        },
    )
    return service._node_from_row(connection, service._node_row(connection, node_id))
