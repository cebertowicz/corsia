"""The user settles a plan row: Mark done or Won't do, whatever it waits on.

The user's word settles the row and everything open beneath it, in one write
(``shared/planning/plans-user-settle-any-2026-09-29.md``). Every stop on a row
exists to keep an agent from acting early -- a hold, an armed watch, an
unfinished prerequisite, a paused plan, a worker still busy -- and none of
them binds the person the plan works for. Refusing left the pane's control
inert: the user clicked, the server answered 409, and nothing moved.

So each open row beneath the one the user chose settles first, deepest first,
with the same outcome, then the row itself: Mark done accepts, Won't do
closes. A delegated row stands its worker down first; the worker retires as
any settled delegation's does, and the route stops a Turn it is still running.
A question put to the user is withdrawn rather than settled, because only its
answer finishes it. A paused plan stays paused: the user settled rows, not the
plan. Settled rows keep their outcome.
"""

from __future__ import annotations

from typing import Any

import plans.boundary_after as after
import plans.errors as errors
import plans.service as service

#: The evidence Mark done persists on each row it accepts.
EVIDENCE = "Marked done by the user."
#: The close reason Won't do records when the pane sends none.
CLOSE_REASON = "Closed by the user"
#: Why a worker's open steps closed when the user marked its step done.
WORKER_STOOD_DOWN = "the user marked the step that delegated this work done"
#: The user's two settling operations and the state each leaves.
OUTCOMES = {"mark_done": "accepted", "close_node": service.CLOSED}
_OPEN = ("pending", "active", "review_requested")


def prepare(
    plan_id: str,
    operation: str,
    params: dict[str, Any],
    *,
    actor,
    mutation_id: str,
    db,
) -> tuple[str, ...]:
    """Authorize a fresh user settlement and name workers it must Stop first.

    A recorded mutation is a replay and needs no repeated Stop. Fresh requests
    perform the mutation boundary's scope, node and revision checks before any
    worker is touched; the mutation repeats them after Stop inside its write.
    """
    from plans import boundary, boundary_fields, receipts

    if actor.kind != "user" or operation not in OUTCOMES:
        return ()
    boundary_fields.validate_operation_params(operation, params)
    mutation_id = boundary._required_mutation_id(mutation_id)
    actor = boundary._revalidate_actor(db, actor)
    plan = boundary._authorize_plan_scope(db, plan_id, actor)
    if plan["owner_kind"] != "thread":
        raise PermissionError("public scope plan mutation is unavailable")
    replay = receipts.load(
        db,
        plan_id=plan_id,
        mutation_id=mutation_id,
        actor_id=actor.audit_id,
        operation=operation,
        fingerprint=receipts.request_fingerprint(operation, params),
    )
    if replay is not None:
        return ()
    boundary._authorize_thread_mutation_owner(
        db, plan, operation, params, actor, review_operation=False,
    )
    boundary._authorize_node(db, plan, params, operation, by_user=True)
    if operation == "mark_done":
        boundary._authorize_user_task(actor)
    return tuple(workers_to_retire(params, db=db))


def workers_to_retire(params: dict[str, Any], *, db) -> list[str]:
    """Return the live worker family this exact user request would retire."""
    node_id = params["node_id"]
    errors.require_node_revision(
        service._node_row(db, node_id), params["expected_revision"],
    )
    below = _open_below(db, node_id)
    return _live(db, _workers(db, [*below, node_id]))


def settle(
    operation: str,
    params: dict[str, Any],
    verified_by: str,
    common: dict,
    *,
    evidence: str = EVIDENCE,
) -> dict:
    """Settle the row and every open row beneath it after every worker stopped.

    ``verified_by`` and ``evidence`` record whose word accepted it: the user's
    own, or the system's observation of the person (``plans.system_user_steps``).
    """
    from plans import admission, user_steering

    db, node_id = common["db"], params["node_id"]
    workers = workers_to_retire(params, db=db)
    _require_workers_stopped(db, workers)
    reason = (params.get("reason") or "").strip() or CLOSE_REASON
    below = _open_below(db, node_id)
    for row_id in below:
        _settle_one(db, row_id, operation, reason, (verified_by, evidence), common)
        admission.release(row_id, OUTCOMES[operation], db=db)
    _settle_one(db, node_id, operation, reason, (verified_by, evidence), common)
    if operation == "mark_done":
        result = after.after_transition(node_id, "accepted", common)
    else:
        result = after.after_close(node_id, service.CLOSED, params, common)
    # Last: a wake the settle sends (``plan_wake``) materializes the owner's
    # pending digest, and this line belongs in the next one it reads. A system
    # settle has no actor; the owner reads it as its own receipt of the
    # transition (``plan_events.append_node_transition``), not as a user act.
    if common["actor_id"] is not None:
        user_steering.tell_owner(
            operation, {"node_id": node_id, "reason": reason}, common, below=len(below),
        )
    return result


def _settle_one(
    db, node_id: str, operation: str, reason: str, word: tuple[str, str], common: dict,
) -> None:
    """One row: a question is withdrawn, anything else stands its worker down and settles."""
    from plans import decision_lifecycle, node_settlement

    if node_settlement.owning_lane(db, node_id) == node_settlement.DECISION:
        decision_lifecycle.withdraw_for_review_node(
            db, node_id, actor_id=common["actor_id"], provenance=common["provenance"],
        )
        return
    _stand_down(db, node_id, operation, common)
    if operation == "close_node":
        from plans import closure

        closure.close_node(
            node_id, reason=reason, actor_id=common["actor_id"],
            provenance=common["provenance"], db=db,
        )
        return
    _accept(db, node_id, word, common)
    decision_lifecycle.withdraw_for_source(
        db, node_id, reason=decision_lifecycle.SOURCE_DONE,
        actor_id=common["actor_id"], provenance=common["provenance"],
    )


def _accept(db, node_id: str, word: tuple[str, str], common: dict) -> None:
    """Clear the row's stops, record the settling word as evidence, accept.

    A row already awaiting review keeps the evidence it has, with the user's
    word appended as the verdict; one with none takes the user's word as its
    evidence, so acceptance never refuses for want of it.
    """
    from plans import review_evidence

    verified_by, evidence = word
    clear_stops(db, node_id, common)
    if service._node_row(db, node_id)["state"] == "pending":
        service.transition_node(node_id, "active", **common)
    if service._node_row(db, node_id)["state"] == "active":
        review_evidence.submit(
            node_id, evidence=evidence, expected_revision=None,
            paused_plan_binds=False, **common,
        )
    else:
        review_evidence.record_verdict(node_id, evidence, verdict="Accepted", **common)
    service.transition_node(
        node_id, "accepted", verification_result="passed",
        verified_by=verified_by, **common,
    )


def clear_stops(db, node_id: str, common: dict) -> None:
    """End every hold, unmet prerequisite and armed watch through its owner.

    Also the owner agent's settle of a person's step (``plans.user_step_settle``),
    which refuses a pause the user owns before it gets here.
    """
    from plans import archival, dependencies, holds, node_waits

    for hold in holds.active_holds(node_id, db=db):
        holds.clear(
            node_id, hold["id"], expected_kind=hold["kind"],
            expected_revision=service._node_row(db, node_id)["revision"], **common,
        )
    for prerequisite in node_waits.unmet_prerequisites(db, [node_id], inherit=False).get(node_id, []):
        dependencies.remove_dependency(node_id, prerequisite["id"], **common)
    archival.settle_watches(db, node_id)


def _stand_down(db, node_id: str, operation: str, common: dict) -> None:
    """Close the open steps of the worker this row handed work to.

    The user's word on the row is the verdict on the delegation, so nothing the
    worker is still doing for it holds that verdict back.
    """
    from plans import worker_retirement

    node = service._node_row(db, node_id)
    if node["spawned_thread_id"] is None:
        return
    plan = db.execute(
        """SELECT id FROM plans
           WHERE owner_kind='thread' AND owner_id=? AND parent_plan_id=?""",
        (node["spawned_thread_id"], node["plan_id"]),
    ).fetchone()
    if plan is None:
        return
    worker_retirement.close_open_steps(
        db, plan["id"], common["actor_id"],
        reason=(
            WORKER_STOOD_DOWN if operation == "mark_done"
            else worker_retirement.CLOSED_WITH_DELEGATION
        ),
    )


def _open_below(db, node_id: str) -> list[str]:
    """The open rows beneath this one that are still in the work, deepest first."""
    from plans import node_states

    marks = ",".join("?" for _ in _OPEN)
    return [
        str(row["id"]) for row in db.execute(
            "WITH RECURSIVE below(id,state,depth,order_key) AS ("
            " SELECT n.id,n.state,1,n.order_key FROM plan_nodes n"
            f" WHERE n.parent_id=? AND {node_states.in_work_sql('n')}"
            " UNION ALL SELECT n.id,n.state,below.depth+1,n.order_key"
            " FROM plan_nodes n JOIN below ON n.parent_id=below.id"
            f" WHERE {node_states.in_work_sql('n')})"
            f" SELECT id FROM below WHERE state IN ({marks})"
            " ORDER BY depth DESC, order_key",
            (node_id, *_OPEN),
        ).fetchall()
    ]


def _workers(db, node_ids: list[str]) -> list[str]:
    """Every worker these rows hand work to, and the workers those hand work to."""
    found: list[str] = []
    frontier = [
        row["spawned_thread_id"] for row in db.execute(
            f"SELECT spawned_thread_id FROM plan_nodes WHERE id IN "
            f"({','.join('?' for _ in node_ids)}) AND spawned_thread_id IS NOT NULL",
            node_ids,
        ).fetchall()
    ]
    while frontier:
        worker = frontier.pop()
        if worker in found:
            continue
        found.append(worker)
        frontier.extend(
            row["spawned_thread_id"] for row in db.execute(
                """SELECT n.spawned_thread_id FROM plan_nodes n
                   JOIN plans p ON p.id=n.plan_id
                   WHERE p.owner_kind='thread' AND p.owner_id=?
                     AND n.spawned_thread_id IS NOT NULL""",
                (worker,),
            ).fetchall()
        )
    return found


def _lifecycle(db, worker: str) -> str | None:
    row = db.execute(
        "SELECT COALESCE(lifecycle_state,'active') AS state FROM threads WHERE id=?",
        (worker,),
    ).fetchone()
    return None if row is None else row["state"]


def _live(db, workers: list[str]) -> list[str]:
    return [w for w in workers if _lifecycle(db, w) in {"active", "dormant"}]


def _require_workers_stopped(db, workers: list[str]) -> None:
    """Refuse every plan write while a worker this settlement retires still runs."""
    if not workers:
        return
    marks = ",".join("?" for _ in workers)
    running = db.execute(
        f"SELECT 1 FROM active_turns WHERE thread_id IN ({marks}) "
        "AND status='running' LIMIT 1",
        workers,
    ).fetchone()
    if running is not None:
        raise service.PlanError(
            "The worker is still running, so the task was not settled. Try again."
        )
