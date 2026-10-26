"""Structured child-plan attachment and plan-state-driven harvest."""

from __future__ import annotations

import json
import os

import plans.delegation_adoption as delegation_adoption
import plans.errors as errors
import plans.notifications as notifications
import plans.plan_wake as plan_wake
import plans.service as service

_OPEN_PARENT_STATES = ("pending", "active", "review_requested")

#: States a node can be harvested from. A delegated step's execution belongs
#: to its worker, not the parent's one slot, so a step the parent never started
#: (fan-out while the slot holds other work) returns like a started one (#2199).
_HARVESTABLE_STATES = ("pending", "active", "review_requested")


def _live_attachment(states=_OPEN_PARENT_STATES, alias: str = "") -> tuple[str, tuple]:
    """Return the one definition of a *live* delegation attachment.

    A node owns a live delegation exactly while it is **unarchived** and its own
    state is still open. Both halves are load-bearing: no archive writer touches
    `state`, so an archived node keeps its open state forever, and a predicate
    missing the archive half sees an invisible row that permanently refuses
    re-delegating the child and can be harvested into a transition nobody sees.

    Returned as a SQL fragment plus its bound state parameters, so every
    predicate shares this definition instead of copying it. A joined read passes
    the trusted node alias.
    """
    placeholders = ",".join("?" for _ in states)
    prefix = f"{alias}." if alias else ""
    return f"{prefix}archived_at IS NULL AND {prefix}state IN ({placeholders})", tuple(states)


def _is_live_attachment(node, states=_OPEN_PARENT_STATES) -> bool:
    """Row-level twin of :func:`_live_attachment`, for an already-loaded node."""
    return node["archived_at"] is None and node["state"] in states


def attachment_payload(event) -> dict:
    """Decode one durable spawned-thread attachment event payload."""
    try:
        payload = json.loads(event["payload_json"] or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise service.PlanError("invalid structured attachment payload") from exc
    if not isinstance(payload, dict):
        raise service.PlanError("invalid structured attachment payload")
    return payload


def _expected_parent(plan):
    """Return the thread whose child may attach to a node in this plan: its owner."""
    return plan["owner_id"] if plan["owner_kind"] == "thread" else None


def _valid_structured_epoch(node, db):
    if not _is_live_attachment(node):
        return None
    event = _latest_attachment(node["id"], db)
    if event is None:
        return None
    try:
        payload = attachment_payload(event)
    except service.PlanError:
        return None
    child_id = payload.get("spawned_thread_id")
    child_plan_id = payload.get("child_plan_id")
    floor = payload.get("event_floor_revision")
    if (
        not isinstance(child_id, str)
        or not child_id
        or child_id != node["spawned_thread_id"]
        or not isinstance(child_plan_id, str)
        or not child_plan_id
        or type(floor) is not int
        or floor < 1
    ):
        return None
    parent_plan = db.execute(
        "SELECT * FROM plans WHERE id=?", (node["plan_id"],)
    ).fetchone()
    child_plan = db.execute(
        "SELECT * FROM plans WHERE id=?", (child_plan_id,)
    ).fetchone()
    expected_parent = (
        _expected_parent(parent_plan) if parent_plan is not None else None
    )
    child = db.execute(
        "SELECT project_id,spawned_by FROM threads WHERE id=?", (child_id,)
    ).fetchone()
    parent = db.execute(
        "SELECT project_id FROM threads WHERE id=?", (expected_parent,)
    ).fetchone()
    if (
        child_plan is None
        or child_plan["owner_kind"] != "thread"
        or child_plan["owner_id"] != child_id
        or child_plan["parent_plan_id"] != node["plan_id"]
        or child_plan["revision"] < floor
        or child is None
        or parent is None
        or child["spawned_by"] != expected_parent
        or child["project_id"] != parent["project_id"]
        or node["spawn_kind"] is None
    ):
        return None
    live, states = _live_attachment()
    duplicate = db.execute(
        f"""SELECT 1 FROM plan_nodes
            WHERE spawned_thread_id=? AND id<>? AND {live} LIMIT 1""",
        (child_id, node["id"], *states),
    ).fetchone()
    return None if duplicate is not None else (event, payload)


def _reject_other_open_attachment(child_id: str, node_id: str, *, db) -> None:
    live, states = _live_attachment()
    row = db.execute(
        f"""SELECT id FROM plan_nodes
            WHERE spawned_thread_id=? AND id<>? AND {live} LIMIT 1""",
        (child_id, node_id, *states),
    ).fetchone()
    if row is not None:
        raise service.PlanError(
            "child already has an open structured attachment"
        )


def _require_spawned_by_owner(connection, plan, spawned_thread_id: str) -> None:
    """Refuse a worker this plan's owner did not spawn, or one in another project.

    The one exception is an orphan of this plan's own delegation, which
    ``delegation_adoption`` vets here and re-points in ``attach_spawned_thread``.
    """
    expected_parent = _expected_parent(plan)
    if expected_parent is None:
        raise service.PlanError("spawn attachment requires a concrete owner thread")
    parent = connection.execute(
        "SELECT project_id FROM threads WHERE id=?", (expected_parent,)
    ).fetchone()
    child = connection.execute(
        "SELECT project_id,spawned_by FROM threads WHERE id=?",
        (spawned_thread_id,),
    ).fetchone()
    if child is None:
        raise service.PlanError("spawned thread is not owned by this plan node")
    if child["spawned_by"] != expected_parent:
        delegation_adoption.require_orphan(
            connection, plan, spawned_thread_id, child["spawned_by"],
        )
    if parent is None or child["project_id"] != parent["project_id"]:
        raise service.PlanError("spawn attachment is cross-project")


def _release_delegated_selection(connection, node, actor_id, provenance) -> None:
    """Hand the slot on when the attachment took this step's eligibility away.

    The child plan is doing the step's work now, which is one of the step's
    admission causes, so a selection it invalidates is released and the plan
    reconciles inside this same write. An attachment that changes nothing --
    a child with nothing running, a step nobody had selected -- reconciles
    nothing: it made no other task newly eligible.
    """
    from plans import admission, admission_attempts, selection_validity

    attempt = admission_attempts.open_attempt(connection, str(node["id"]))
    if attempt is None or selection_validity.selection_valid(connection, attempt):
        return
    admission.release(str(node["id"]), "spawned_thread_attached", db=connection)
    admission.reconcile_plan(
        str(node["plan_id"]), actor_id=actor_id, provenance=provenance, db=connection,
    )


def attach_spawned_thread(
    node_id: str,
    spawned_thread_id: str,
    *,
    expected_revision: int | None = None,
    actor_id: str | None = None,
    provenance: str = "plan_mutation",
    db=None,
):
    """Attach a direct child plan with one durable delegation epoch floor.

    A node has one worker. Attaching a different one over a worker the node
    still names is refused: handing the step over must also end the old
    worker's open steps, which is ``replace_spawned_thread``. A worker whose
    delegator was replaced off this plan's step is adopted first
    (``delegation_adoption``).
    """
    with service._connection(db) as connection, service._write(connection):
        node = service._live_node_row(connection, node_id)
        if expected_revision is not None:
            errors.require_node_revision(node, expected_revision)
        current = node["spawned_thread_id"]
        if current is not None and current != spawned_thread_id:
            raise service.PlanInputError(
                f"node already delegates to {current}; use replace_spawned_thread"
                f" to hand it to {spawned_thread_id}"
            )
        plan = service._plan_row(connection, node["plan_id"])
        _require_spawned_by_owner(connection, plan, spawned_thread_id)
        from core.thread_lifecycle import reactivate_settled_worker

        reactivate_settled_worker(connection, spawned_thread_id)
        _reject_other_open_attachment(spawned_thread_id, node_id, db=connection)
        delegation_adoption.adopt_orphan(connection, plan, spawned_thread_id)
        child_plan = service.ensure_plan("thread", spawned_thread_id, db=connection)
        if (
            node["spawned_thread_id"] == spawned_thread_id
            and _valid_structured_epoch(node, connection) is not None
        ):
            return service._node_from_row(connection, node)
        if (
            node["spawned_thread_id"] != spawned_thread_id
            or node["spawn_kind"] is None
        ):
            service.touch_node(
                connection, node_id, spawned_thread_id=spawned_thread_id,
                spawn_kind=node["spawn_kind"] or "delegated",
            )
        from plans import commitments

        commitments.inherit_open_child_nodes(
            child_plan.id,
            node_id,
            actor_id=actor_id,
            provenance=provenance,
            db=connection,
        )
        _release_delegated_selection(connection, node, actor_id, provenance)
        _attachment_event(
            connection, node_id, spawned_thread_id, child_plan.id,
            actor_id=actor_id, provenance=provenance,
        )
        from plans import delegated_usage

        delegated_usage.stamp_running_turn(connection, spawned_thread_id)
        return service._node_from_row(
            connection, service._node_row(connection, node_id)
        )


def detach_spawned_thread(
    node_id: str,
    *,
    reason: str,
    expected_revision: int | None = None,
    actor_id: str | None = None,
    provenance: str = "plan_mutation",
    db=None,
):
    """End a node's attachment to its worker: the inverse of attach, and its one writer.

    The node stops naming the worker, so nothing that worker does can wake the
    parent about the step or hold it waiting: both read the attachment. The
    worker's open steps are the caller's to settle, and its plan stays where
    the scope hierarchy puts it. A node with no worker is returned unchanged.
    """
    recorded = (reason or "").strip()
    if not recorded:
        raise service.PlanInputError("detaching a worker requires a reason")
    with service._connection(db) as connection, service._write(connection):
        node = service._live_node_row(connection, node_id)
        if expected_revision is not None:
            errors.require_node_revision(node, expected_revision)
        worker = node["spawned_thread_id"]
        if worker is None:
            return service._node_from_row(connection, node)
        child_plan = connection.execute(
            "SELECT id FROM plans WHERE owner_kind='thread' AND owner_id=?", (worker,),
        ).fetchone()
        service.touch_node(connection, node_id, spawned_thread_id=None)
        service._event(
            connection, node_id, "spawned_thread_detached",
            actor_id=actor_id, provenance=provenance,
            payload={
                "spawned_thread_id": worker,
                "child_plan_id": None if child_plan is None else child_plan["id"],
                "reason": recorded,
            },
        )
        # The step no longer waits on the worker: it is its plan's own work again.
        from plans import admission

        admission.reconcile_plan(
            str(node["plan_id"]), actor_id=actor_id, provenance=provenance,
            causing_executor=actor_id, db=connection,
        )
        return service._node_from_row(
            connection, service._node_row(connection, node_id)
        )


def _attachment_event(
    connection, node_id: str, child_id: str, child_plan_id: str, *, actor_id, provenance,
) -> None:
    """Open one attachment epoch, floored at the child plan's current revision."""
    service._event(
        connection, node_id, "spawned_thread_attached",
        actor_id=actor_id, provenance=provenance,
        payload={
            "spawned_thread_id": child_id,
            "child_plan_id": child_plan_id,
            "event_floor_revision": service._plan_row(connection, child_plan_id)["revision"],
        },
    )


def has_open_structured_attachment(child_thread_id: str, *, db=None) -> bool:
    """Return whether structured delegation owns completion for a validated open epoch."""
    with service._connection(db) as connection:
        live, states = _live_attachment()
        rows = connection.execute(
            f"""SELECT * FROM plan_nodes
                WHERE spawned_thread_id=? AND {live} ORDER BY id""",
            (child_thread_id, *states),
        ).fetchall()
        return any(
            _valid_structured_epoch(node, connection) is not None for node in rows
        )


def _latest_attachment(node_id: str, db):
    return db.execute(
        """SELECT * FROM plan_node_events
           WHERE node_id=? AND event='spawned_thread_attached'
           ORDER BY created_at DESC,rowid DESC LIMIT 1""",
        (node_id,),
    ).fetchone()


def harvest_ready_children(
    *,
    db,
    drives: list[str],
    changed_plan_ids: list[str],
    worker_thread_id: str | None = None,
) -> list[str]:
    """Move returned child epochs to parent review through the one harvest owner."""

    from plans.delegation_harvest import harvest_ready_children as harvest

    return harvest(
        db=db,
        drives=drives,
        worker_thread_id=worker_thread_id,
        changed_plan_ids=changed_plan_ids,
    )
