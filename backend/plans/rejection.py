"""Rejecting a reviewed submission creates its one linked revision task.

The submission stays where it was reviewed, closed as sent back, with its
evidence and the feedback on its events: sent back is not failed, so it never
reads red. Exactly one revision task is created beside it,
linked through ``follow_up_of`` and carrying the current requirements, the
feedback, the assigned worker and the original commitment. Nothing is
invented by a model or remembered by the worker. Repeating a rejection
returns the revision it already created.
"""

from __future__ import annotations

import json
import logging

import plans.boundary_fields as fields
from plans import followups, service
from plans.models import PlanNode
from plans.mutations import _assert_revision

log = logging.getLogger(__name__)


def reject_review(node_id: str, *, feedback: str, expected_revision: int | None = None,
                  actor_id: str | None = None, provenance: str = "plan_mutation", db=None) -> PlanNode:
    """Reject a reviewed node once and return it; its revision is created beside it."""
    rejected, _revision, _replayed = reject_submission(
        node_id, feedback=feedback, expected_revision=expected_revision,
        actor_id=actor_id, provenance=provenance, db=db,
    )
    return rejected


def _replay(connection, node, existing, feedback: str):
    """Return the same rejection for a retry of it; refuse other feedback.

    A replay carries the revision the caller saw before the rejection, so no
    revision evidence is asked of it: nothing moves.
    """
    revision, created_with = existing
    if created_with != feedback:
        # Not a replay: the submission is settled and its revision exists;
        # other feedback belongs to the revision's own review.
        raise service.PlanError(
            "submission already rejected with different feedback; "
            f"the revision is {revision.id}"
        )
    return service._node_from_row(connection, node), revision, True


def reject_submission(
    node_id: str, *, feedback: str, expected_revision: int | None = None,
    actor_id: str | None = None, provenance: str = "plan_mutation", db=None,
) -> tuple[PlanNode, PlanNode, bool]:
    """Send the reviewed submission back and create the one revision task it requires.

    The verdict lands on the submission: it closes as sent back with the
    feedback, its evidence preserved. A delegated step's verdict also lands on
    what the worker returned -- its steps awaiting the verdict are sent back
    with the same feedback -- and the revision is attached to the same worker, whose own
    plan is where it revises. Whatever waited on the submission now waits on
    the revision, whose acceptance is what it needs. Telling whoever now holds
    the revision is the boundary's (``plan_wake.notify_revision``), after the
    release and reconciliation that follow. Returns the rejected task, its revision, and
    whether this was a replay.
    """
    feedback = service._required_text(feedback, "review feedback")
    with service._connection(db) as connection, service._write(connection):
        # A sent-back submission is closed, which no writer may touch; its
        # replay only reads, so it is resolved before the live-node guard.
        # One sent back over open work before schema 57 stays Failed.
        node = service._node_row(connection, node_id)
        existing = _revision_of(connection, node_id)
        if node["state"] in (service.CLOSED, "failed") and existing is not None:
            return _replay(connection, node, existing, feedback)
        service.require_live_node(node)
        _assert_revision(node, expected_revision)
        if node["state"] != "review_requested":
            raise service.PlanError("only review_requested nodes can be rejected")
        from plans import archival, dependencies

        # A closed node may hold nothing open beneath it (``node_states.retired``).
        archival._require_no_open_children(connection, node_id)
        worker = node["spawned_thread_id"]
        returned = []
        if worker is not None:
            from plans import delegation_harvest

            returned = delegation_harvest.returned_steps(connection, node)
        revision = followups.add_follow_up(
            node_id, title=str(node["title"]), acceptance_criteria=str(node["acceptance_criteria"]),
            description=node["description"], spawn_kind=node["spawn_kind"],
            inherit_commitment=True, step="rules", actor_id=actor_id, provenance=provenance,
            db=connection,
        )
        # Before the close drops the submission's edges: what waited on it
        # waits on its revision.
        dependencies.move_dependents(
            connection, node_id, revision.id, actor_id=actor_id, provenance=provenance,
        )
        # The step's worker is not settled: the revision is attached to it below.
        rejected, rejection_event_id = _send_back(
            connection, node_id, feedback, actor_id, provenance, settles_worker=False,
        )
        # The worker's returned steps settle like any closed step: a nested
        # delegation among them retires its own worker, which gets no revision.
        # They carry the verdict for their own closure, but only the reviewed
        # node above is its correction fact.
        for leaf in returned:
            _send_back(
                connection, leaf["id"], feedback, actor_id, provenance, settles_worker=True,
            )
        worker_step_id = _attach_revision_and_seed_worker(
            connection, revision, worker=worker, title=str(node["title"]), feedback=feedback,
            actor_id=actor_id, provenance=provenance,
        )
        payload = {"source_node_id": node_id, "feedback": feedback}
        if worker_step_id is not None:
            payload["worker_step_id"] = worker_step_id
        service._event(
            connection, revision.id, "revision_created", actor_id=actor_id,
            provenance=provenance, payload=payload,
        )
        from core.sent_back_corrections import record_rejection_correction

        # In this write: a fact that cannot be recorded fails the rejection.
        record_rejection_correction(connection, rejection_event_id)
        return rejected, service._node_from_row(connection, service._node_row(connection, revision.id)), False


def _attach_revision_and_seed_worker(
    connection, revision, *, worker: str | None, title: str, feedback: str,
    actor_id: str | None, provenance: str,
) -> str | None:
    """Attach a delegated revision and create its worker-owned revision step."""
    if worker is None:
        return None
    from plans import delegation, intent_words
    from plans.close_next_bounds import REVISION_BRIEF_MAX_BYTES, bounded

    delegation.attach_spawned_thread(
        revision.id, worker, actor_id=actor_id, provenance=provenance, db=connection,
    )
    worker_plan = service.ensure_plan("thread", worker, db=connection)
    worker_step = service.add_node(
        worker_plan.id, kind="task", title=f"Revise: {title}",
        acceptance_criteria=str(revision.acceptance_criteria),
        description=bounded(feedback, REVISION_BRIEF_MAX_BYTES),
        intent_node_id=intent_words.canonical(connection, revision.id)["id"],
        actor_id=actor_id, provenance=provenance, db=connection,
    )
    return worker_step.id


#: What a sent-back submission's close reason starts with; the row shows it.
SENT_BACK = "Sent back"


def _send_back(
    connection, node_id: str, feedback: str, actor_id, provenance, *, settles_worker: bool,
) -> tuple[PlanNode, str]:
    """The verdict on one submission: rejected with this feedback, then closed.

    It closes as ``plans.closure.close_node`` records a close -- the reason on
    the transition, then ``archival.release_held`` -- so a sent-back row reads
    and holds exactly what any closed row does. ``settles_worker`` is False
    only for the rejected step itself, whose worker is attached to the
    revision in this same write; every other submission the verdict lands on
    settles its worker as any closed step.
    """
    from plans import archival

    rejection_event_id = service._event(
        connection, node_id, "review_rejected", actor_id=actor_id,
        provenance=provenance, payload={"feedback": feedback},
    )
    sent_back = service._transition(
        connection, node_id, service.CLOSED, actor_id=actor_id, provenance=provenance,
        verification_result=None, verified_by=None, settles_worker=settles_worker,
        payload={"reason": f"{SENT_BACK}: {feedback}"}, sent_back=True,
    )
    archival.release_held(
        connection, service._node_row(connection, node_id),
        actor_id=actor_id, provenance=provenance,
    )
    return sent_back, rejection_event_id


def _revision_of(connection, node_id: str) -> tuple[PlanNode, str | None] | None:
    """The revision a rejection of this node already created, with its feedback."""
    row = connection.execute(
        """SELECT n.*, e.payload_json AS created_json FROM plan_nodes n
           JOIN plan_node_events e ON e.node_id=n.id AND e.event='revision_created'
           WHERE n.follow_up_of=? AND n.archived_at IS NULL
           ORDER BY n.rowid DESC LIMIT 1""",
        (node_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        created_with = json.loads(row["created_json"] or "{}").get("feedback")
    except (TypeError, json.JSONDecodeError):
        created_with = None
    return service._node_from_row(connection, row), created_with


def _revision_worker_step_id(db, revision_id: str) -> str | None:
    """Return the worker-owned step a delegated rejection seeded, if any."""
    row = db.execute(
        "SELECT payload_json FROM plan_node_events WHERE node_id=? "
        "AND event='revision_created' ORDER BY rowid DESC LIMIT 1",
        (revision_id,),
    ).fetchone()
    try:
        value = json.loads(row["payload_json"] or "{}").get("worker_step_id") if row else None
    except (TypeError, json.JSONDecodeError):
        value = None
    return value if isinstance(value, str) else None


def _revision_round(db, revision) -> int:
    """Return the send-back round that created ``revision``."""
    from plans.operator_node_facts import revisions

    return revisions([revision.plan_id], db=db).get(revision.id, 0)


def _worker_model(db, worker_id: str | None) -> str:
    """Return the worker's currently selected model without inventing one."""
    if worker_id is None:
        return "unknown"
    row = db.execute("SELECT model FROM threads WHERE id=?", (worker_id,)).fetchone()
    return str(row["model"]) if row is not None and row["model"] else "unknown"


def after_rejection(
    node_id: str, revision, feedback: str, common: dict, *, executor: str | None,
) -> tuple[dict, str]:
    """Release the rejected submission's selection, then hand the revision to its holder.

    The one story a rejection tells: not "closed by another actor" plus "your
    current task", but that the submission was rejected and this revision is
    now the holder's -- the worker it is attached to, or the plain step's
    executor -- unless that holder is the actor, whose result already says so.
    Returns the admission result and how the handoff went: ``actor`` (the
    actor holds it), ``delivered``, or ``undelivered`` -- the holder cannot
    take input now, so the revision stays truthfully pending and assigned and
    the missed handoff is on the revision's record, never swallowed.

    A revision handed to its attached worker stays ``pending``, like any
    step its worker works (#2199); the wake queued for the worker is what
    makes it read as the worker's, not the parent's (``wait_causes``, #2052).
    """
    from plans import admission, plan_wake

    db = common["db"]
    admission.release(node_id, "review_rejected", db=db)
    node = service._node_row(db, node_id)
    plan = service._plan_row(db, node["plan_id"])
    admission.reconcile_plan(
        node["plan_id"], actor_id=common["actor_id"],
        provenance=common["provenance"], causing_executor=executor, db=db,
    )
    holder = revision.spawned_thread_id or admission.resolve_executor(db, node, plan)
    handoff = "actor"
    if holder and holder != executor:
        delivered = plan_wake.notify_revision(
            db, revision.id, recipient=holder, feedback=feedback,
            worker_step_id=_revision_worker_step_id(db, revision.id),
        )
        handoff = "delivered" if delivered else "undelivered"
        if not delivered:
            with service._write(db):
                service._event(
                    db, revision.id, "revision_handoff_undelivered",
                    actor_id=common["actor_id"], provenance=common["provenance"],
                    payload={"holder": holder},
                )
            log.warning(
                "rejection: revision %s could not be handed to %s; it stays pending",
                revision.id, holder,
            )
    return admission.result(node_id, db=db), handoff


def dispatch_reject(node_id: str, params: dict, common: dict, *, executor: str | None) -> dict:
    """The boundary's ``reject``: the mutation, its follow-through, and the result.

    A replayed rejection moved nothing and reports the current admission
    without releasing, re-admitting or waking anything again. The result
    names the one linked revision task by identity, and how its handoff went.
    """
    from plans import admission

    feedback = fields.text(params, "feedback")
    _rejected, revision, replayed = reject_submission(
        node_id, feedback=feedback, **fields.expected(params), **common,
    )
    if replayed:
        result, handoff = admission.result(node_id, db=common["db"]), "replayed"
    else:
        result, handoff = after_rejection(node_id, revision, feedback, common, executor=executor)
    result["revision"] = {
        "node_id": revision.id, "revision": int(revision.revision),
        "source_node_id": node_id, "handoff": handoff,
    }
    round_number = _revision_round(common["db"], revision)
    if round_number >= 2:
        from prompts.plan_tools import send_back_round_guidance

        result["guidance"] = send_back_round_guidance(
            round_number, _worker_model(common["db"], revision.spawned_thread_id),
        )
    return result
