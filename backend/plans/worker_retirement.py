"""Retire a temporary worker once the step that spawned it settles.

The parent's verdict on a delegated step -- accepted, failed as a
cancellation, or closed as no longer wanted -- leaves the worker with nothing
to do for it. Its task-specific
watches and schedules are retired, the artifacts it owned pass to the thread
that delegated to it, and the worker is archived through the one owner of a
thread's lifecycle. Archiving is not evidence deletion: the worker's plan, its
events and its review evidence stay where they are.
"""

from __future__ import annotations

import plans.service as service

CANCELLED = "plan.assignment_cancelled"

#: Why a worker's open step closed when the step that delegated it closed.
CLOSED_WITH_DELEGATION = "the step that delegated this work was closed"
#: Why a worker's open step closed when the parent accepted without it (#1866).
STOOD_DOWN = "the parent accepted the step that delegated this work without it"
#: Retirement trigger for taskless registrations an accepted task leaves behind.
TASKLESS_TRIGGER = "later_task_accepted"


def settle_terminal(
    connection, node_id: str, new_state: str, *, event_id: str | None,
    actor_id: str | None, provenance: str, settles_worker: bool,
) -> None:
    """What a terminal transition retires: registrations, a closed task's questions, the worker.

    An accepted task also retires the taskless registrations its executor
    made before starting it. A failed, closed or never-selected task is not a
    completion and retires none of those. Registrations the registry cannot
    classify now never fail the transition: it leaves them for the next start.

    A rejection fails the submission but hands the same worker its revision
    in the same write, so it passes ``settles_worker=False`` and the worker
    is left be; every other terminal verdict settles the worker too.
    """
    from plans import admission_attempts, user_ask
    from plans.decision_lifecycle import withdraw_for_closed_source
    from registry.node_retirement import (
        retire_registrations_for_node, retire_taskless_rows,
    )
    from registry.startup_backfill import settlement_retirement

    with settlement_retirement(connection):
        retire_registrations_for_node(
            connection, node_id=node_id, trigger=new_state, causal_event_id=event_id,
        )
        start = (
            admission_attempts.executor_start(connection, node_id)
            if new_state in service._ACCEPTED_STATES else None
        )
        if start is not None:
            retire_taskless_rows(
                connection, thread_id=start[0], started_at=start[1],
                trigger=TASKLESS_TRIGGER, causal_event_id=event_id,
            )
    user_ask.withdraw_node_asks(
        connection, node_id, actor_id=actor_id, provenance=provenance,
    )
    withdraw_for_closed_source(
        connection, node_id, new_state, actor_id=actor_id, provenance=provenance,
    )
    if settles_worker:
        retire_settled_worker(
            connection, node_id, actor_id=actor_id, provenance=provenance,
            causal_event_id=event_id,
        )


def retire_settled_worker(
    connection, node_id: str, *, actor_id: str | None, provenance: str,
    causal_event_id: str | None,
) -> bool:
    """Retire the worker a settled step spawned, if nothing else still holds it."""
    node = service._node_row(connection, node_id)
    worker = node["spawned_thread_id"]
    if worker is None:
        return False
    thread = connection.execute(
        "SELECT spawned_by, agent_enabled FROM threads WHERE id=?", (worker,),
    ).fetchone()
    if (
        thread is None
        or thread["spawned_by"] is None
        or thread["agent_enabled"]
        or _held_elsewhere(connection, worker, node_id)
    ):
        return False
    plan = connection.execute(
        """SELECT id FROM plans
           WHERE owner_kind='thread' AND owner_id=? AND parent_plan_id=?""",
        (worker, node["plan_id"]),
    ).fetchone()
    if plan is not None:
        if node["state"] == "failed":
            _suppress_open_steps(connection, plan["id"], actor_id)
        elif node["state"] == service.CLOSED:
            close_open_steps(
                connection, plan["id"], actor_id, reason=CLOSED_WITH_DELEGATION,
            )
        from plans import projections

        if projections.open_delegated_work(connection, plan["id"]):
            return False
    _retire_watches(connection, worker)
    _retire_schedules(connection, worker)
    _hand_over_artifacts(connection, worker, thread["spawned_by"], causal_event_id)
    from core.thread_lifecycle import archive_settled_worker

    return archive_settled_worker(connection, worker)


def _held_elsewhere(connection, worker: str, node_id: str) -> bool:
    return connection.execute(
        """SELECT 1 FROM plan_nodes
           WHERE spawned_thread_id=? AND id<>? AND archived_at IS NULL
             AND state IN ('pending','active','review_requested') LIMIT 1""",
        (worker, node_id),
    ).fetchone() is not None


def _suppress_open_steps(
    connection, plan_id: str, actor_id: str | None, *, provenance: str = CANCELLED,
) -> None:
    """A cancelled assignment fails the worker's open steps; their evidence stays.

    Leaves first, then any started step left with nothing open beneath it
    (``projections.open_delegated_node_ids``). ``provenance`` is the
    cancellation's, unless a schema step applies this verdict late and must
    tell no one.
    """
    from plans import projections

    while open_ids := projections.open_delegated_node_ids(connection, plan_id):
        for node_id in open_ids:
            service._transition(
                connection, node_id, "failed", actor_id=actor_id,
                provenance=provenance, verification_result=None, verified_by=None,
            )


def close_open_steps(
    connection, plan_id: str, actor_id: str | None, *, reason: str,
    provenance: str = CANCELLED,
) -> None:
    """The worker's open steps are not wanted any more, so they close.

    Either the step that delegated them closed, or the parent accepted it
    without them. Closed, not failed: nothing was attempted and lost. Each
    close takes its step out of the work, which can leave a parent with no
    child in work as a new open leaf, so the pass repeats until the plan holds
    none, a started step whose own steps settled included
    (``projections.open_delegated_node_ids``). A step that delegated further
    closes its own worker the same way, through this same path.
    """
    from plans import closure, projections

    while open_ids := projections.open_delegated_node_ids(connection, plan_id):
        for node_id in open_ids:
            closure.close_node(
                node_id, reason=reason, actor_id=actor_id,
                provenance=provenance, db=connection,
            )


def _retire_watches(connection, worker: str) -> None:
    from automation import watches

    for row in connection.execute(
        "SELECT id FROM expected_event_watches WHERE owner_thread_id=? AND status='armed'",
        (worker,),
    ).fetchall():
        watches.resolve_watch(row["id"], db=connection)


def _retire_schedules(connection, worker: str) -> None:
    from automation import schedules

    schedules.retire_for_thread(worker, service._now(), db=connection)


def _hand_over_artifacts(connection, worker: str, parent: str, causal_event_id) -> None:
    """Continuing obligations pass to the delegating thread as their triage owner.

    A parent-chain subscriber cannot own the artifact outright -- the registry
    keeps that invariant -- so the worker's ownership is released and the
    parent named as triage: its events route to the parent, accountably, until
    someone claims them.
    """
    from registry import ownership

    rows = connection.execute(
        """SELECT a.integration, a.type, a.external_id
           FROM artifact_registrations r JOIN artifact_identities a ON a.id=r.artifact_id
           WHERE r.parent_thread_id=? AND a.state='active'
             AND r.relationship_state IN ('owner-active','owner-pinned')
           ORDER BY a.id""",
        (worker,),
    ).fetchall()
    for row in rows:
        ownership.release_artifact(
            **dict(row), actor_thread_id=worker, authority="plan",
            reason="assignment settled; obligations pass to the delegating thread",
            source_path="plans.worker_retirement", triage_thread_id=parent,
            causal_event_id=causal_event_id, db=connection,
        )
