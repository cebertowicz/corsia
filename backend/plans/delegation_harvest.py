"""Harvest: a returned child epoch becomes one parent review and one wake.

The attachment side of structured delegation lives in ``plans.delegation``;
this module owns what happens once the worker has returned. It reaches into
that module's epoch helpers by the package's intra-module convention rather
than copying them.
"""

from __future__ import annotations

import plans.node_states as node_states
import plans.notifications as notifications
import plans.plan_wake as plan_wake
import plans.plan_write as plan_write
import plans.service as service
from plans.harvest_review_event import HARVEST_EVENT, latest_review_event_sql
from plans.delegation import (
    _HARVESTABLE_STATES,
    _live_attachment,
    _valid_structured_epoch,
    attachment_payload,
)


def _ready(node, payload, db) -> bool:
    from plans import admission_attempts, projections

    child = service._plan_row(db, payload["child_plan_id"])
    if child["revision"] <= payload["event_floor_revision"]:
        return False
    # A return-shaped plan is transient while its worker's Turn still runs:
    # that Turn may add its real next step. The existing live-Turn reader is
    # the one owner of this fact; terminal errors are already out of 'running'.
    if admission_attempts.turn_binding(db, payload["spawned_thread_id"]) is not None:
        return False
    # The worker's final return -- its last step put up for review -- is what
    # the parent judges. Waiting for the verdict before harvesting would need
    # the parent to act before the wake that lets it act.
    return (
        projections.delegated_work_returned(db, child["id"])
        and node["state"] in ("pending", "active")
        and _returned_since(db, node, child["id"])
    )


def _returned_since(connection, node, child_plan_id: str) -> bool:
    """Whether the worker's return is work for this attachment and these requirements.

    A return answers the step as it stands: the attachment that handed it
    over and the requirements the parent last set. A step the worker settled
    before either (a reused worker's old step, or work against criteria the
    parent has since restated) is not a return to judge (#2962), so the
    parent keeps its step until the worker settles something after them.
    The requirements were set at creation (revision 1) or by the first
    restatement carrying the current revision; a later title-only edit
    changes no requirement. Event rowids
    order the events, so same-instant writes still compare.
    """
    boundary = connection.execute(
        """SELECT MAX(
             COALESCE((SELECT MAX(rowid) FROM plan_node_events
              WHERE node_id=:node AND event='spawned_thread_attached'), 0),
             COALESCE((SELECT MIN(rowid) FROM plan_node_events
              WHERE node_id=:node AND event='node_restated' AND :rev>1
                AND json_extract(payload_json,'$.requirements_revision')=:rev), 0)
           ) AS at""",
        {"node": node["id"], "rev": node["requirements_revision"]},
    ).fetchone()["at"]
    ids = [row["id"] for row in return_rows(connection, child_plan_id)]
    if not ids:
        return False
    marks = ",".join("?" * len(ids))
    return connection.execute(
        f"""SELECT 1 FROM plan_node_events
            WHERE node_id IN ({marks}) AND event='state_transition' AND rowid>?
            LIMIT 1""",
        (*ids, boundary),
    ).fetchone() is not None


def return_rows(connection, plan_id: str) -> list:
    """The steps a delegated plan's return is made of, in plan order.

    A worker's return is its leaves. Closed steps are out of the work
    (``node_states.work_ids``), so a plan whose remaining work was all closed
    has no leaf left; its closed steps are the return then, and the parent
    reads each as ``closed``. A plan that never held a step has neither, and
    returned nothing.
    """
    leaves = service._plan_leaf_rows(connection, plan_id)
    if leaves:
        return leaves
    return connection.execute(
        """SELECT n.* FROM plan_nodes n
           WHERE n.plan_id=? AND n.state=? AND n.archived_at IS NULL
             AND NOT EXISTS (
               SELECT 1 FROM plan_nodes child
               WHERE child.parent_id=n.id AND child.state=?
                 AND child.archived_at IS NULL
             )
           ORDER BY n.order_key,n.id""",
        (plan_id, service.CLOSED, service.CLOSED),
    ).fetchall()


def awaiting_owner_verdict(connection, node) -> bool:
    """Whether a delegated step is up for review for its own plan's owner to judge.

    The harvest moves a step into review when the worker attached to it
    returns; the owner of the plan holding that step is who judges the worker,
    so the step is not yet the owner's own return. It stays so until the owner
    acts: submitting review evidence is the owner's return. Judging the
    latest of the two events keeps a later, owner-made review from reading as
    the harvest's.
    """
    return harvest_review(connection, node) is not None


def harvest_review(connection, node):
    """The harvest's review transition, when it is still the step's latest review event."""
    if node["state"] != "review_requested" or not node["spawned_thread_id"]:
        return None
    latest = connection.execute(
        latest_review_event_sql(
            "review_event.id,review_event.event,review_event.provenance,"
            "review_event.created_at", "?",
        ),
        (node["id"],),
    ).fetchone()
    harvested = latest is not None and (latest["event"], latest["provenance"]) == HARVEST_EVENT
    return latest if harvested else None


def returned_steps(connection, node) -> list:
    """The worker's steps the latest return of this review returned.

    A review the parent judges was produced by one return: the first harvest,
    or a later worker round recorded by ``record_round_return`` (both are a
    ``child_plan_completed`` event; only the first carries
    ``attachment_event_id``). The steps up for review at that return are the
    submission, and the verdict -- acceptance or rejection -- lands on exactly
    them. Work the worker has put up for review since is not part of this
    verdict.
    """
    harvest = connection.execute(
        """SELECT created_at,payload_json FROM plan_node_events
           WHERE node_id=? AND event='child_plan_completed'
           ORDER BY created_at DESC,rowid DESC LIMIT 1""",
        (node["id"],),
    ).fetchone()
    if harvest is None:
        return []
    child_plan_id = attachment_payload(harvest)["child_plan_id"]
    returned = []
    for leaf in service._plan_leaf_rows(connection, child_plan_id):
        if leaf["state"] != "review_requested":
            continue
        entered = connection.execute(
            """SELECT created_at FROM plan_node_events
               WHERE node_id=? AND new_state='review_requested'
               ORDER BY created_at DESC,rowid DESC LIMIT 1""",
            (leaf["id"],),
        ).fetchone()
        if entered is not None and entered["created_at"] <= harvest["created_at"]:
            returned.append(leaf)
    return returned


def parent_reviews(connection, plan) -> frozenset[str]:
    """The review steps in this plan that its delegating parent judges.

    The worker owns its steps until it returns; the parent judges what came
    back (matrix section G). A plain review step is the parent's when the
    latest return handed it back (``returned_steps``), or when the worker has
    nothing left it can move, since that review is then the return itself --
    the first one, or a later round's. Every other review step is the
    worker's own, so the work behind it can move. A step whose own worker
    returned and that awaits this worker's verdict (``awaiting_owner_verdict``),
    or one that waits on the user, is never the parent's.
    A plan nobody delegated has none.
    """
    from plans import projections

    worker = str(plan["owner_id"])
    if not projections.live_delegation(connection, worker, plan["parent_plan_id"]):
        return frozenset()
    reviews = {
        str(leaf["id"])
        for leaf in service._plan_leaf_rows(connection, plan["id"])
        if leaf["state"] == "review_requested"
        and not awaiting_owner_verdict(connection, leaf)
        and (leaf["assignee"] or "") != "user"
    }
    if not reviews or not projections.movable_work(connection, plan["id"]):
        return frozenset(reviews)
    step = projections.delegated_step(connection, plan["parent_plan_id"], worker)
    returned = {str(leaf["id"]) for leaf in returned_steps(connection, step)}
    return frozenset(reviews & returned)


def waiting_on_answer(connection, plan_id: str) -> bool:
    """Whether this worker plan waits on an answer someone else owes it.

    Either an open leaf waits on the user, such as a raised decision, or the
    worker is blocked on an input its parent agent has (``delegation_needs``).
    Not the worker's to move, and not finished either, so it holds the return
    (matrix G10, G22): the parent judges the work once the answer has landed.
    """
    from plans import delegation_needs, service

    if delegation_needs.open_for(service._plan_row(connection, plan_id), db=connection):
        return True
    return connection.execute(
        f"""SELECT 1 FROM plan_nodes w
           WHERE w.plan_id=? AND w.archived_at IS NULL AND w.assignee='user'
             AND w.state IN ('pending','active','review_requested')
             AND NOT {node_states.has_child_in_work_sql('w')}
           LIMIT 1""",
        (plan_id,),
    ).fetchone() is not None


def _harvested(node_id: str, attachment_id: str, db) -> bool:
    rows = db.execute(
        """SELECT payload_json FROM plan_node_events
           WHERE node_id=? AND event='child_plan_completed'""",
        (node_id,),
    ).fetchall()
    for row in rows:
        try:
            payload = attachment_payload(row)
        except service.PlanError:
            continue
        if payload.get("attachment_event_id") == attachment_id:
            return True
    return False


def _latest_child_event(child_plan_id: str, db):
    return db.execute(
        """SELECT e.id FROM plan_node_events e
           JOIN plan_nodes n ON n.id=e.node_id
           WHERE n.plan_id=? ORDER BY e.created_at DESC,e.rowid DESC LIMIT 1""",
        (child_plan_id,),
    ).fetchone()


def _hand_on_parent_slot(node, db) -> None:
    """Release a harvested parent's attempt and reconcile its plan in the same write."""
    from plans import admission

    admission.release(node["id"], "review_requested", db=db)
    admission.reconcile_plan(
        node["plan_id"], actor_id=None, provenance="plan.delegation_harvest", db=db,
    )


def record_round_return(
    connection,
    *,
    node,
    child_plan_id: str,
    parent_plan_id: str,
    worker_thread_id: str,
    turn_id: str,
    changed_plan_ids: list[str] | None = None,
) -> bool:
    """Record one post-harvest worker Turn as the parent's next verdict boundary.

    The Turn id is the return epoch. Replaying the terminal hook therefore does
    not advance the parent step twice. The step stays in review; only its
    revision and latest ``child_plan_completed`` boundary move, so the owner's
    next verdict settles exactly the leaves returned in this round.

    Like ``_harvest``, the boundary is recorded only with the wake that reports
    it: a parent that cannot take the wake leaves the step whole (False) for
    the next Turn end, never a boundary the parent was not told about.
    """
    from plans.delegation_notice import _wake_parent

    recorded = connection.execute(
        """SELECT 1 FROM plan_node_events
           WHERE node_id=? AND event='child_plan_completed'
             AND json_extract(payload_json,'$.child_turn_id')=?
           LIMIT 1""",
        (node["id"], turn_id),
    ).fetchone()
    if recorded is not None:
        return False

    connection.execute("SAVEPOINT round_return")
    service.touch_node(connection, node["id"])
    service._event(
        connection, node["id"], "child_plan_completed", actor_id=None,
        provenance="plan.delegation_harvest",
        payload={"child_plan_id": child_plan_id, "child_turn_id": turn_id},
    )
    told = _wake_parent(
        connection, parent_plan_id, worker_thread_id,
        kind="child_round_returned", subject_id=turn_id, revision=0,
    )
    if told is None:
        connection.execute("ROLLBACK TO round_return")
    connection.execute("RELEASE round_return")
    if told is None:
        return False
    if changed_plan_ids is not None and parent_plan_id not in changed_plan_ids:
        changed_plan_ids.append(parent_plan_id)
    return True


def _harvest(node, attachment, payload, child_event, db, reply: str = "") -> bool:
    """Harvest one return as a unit: event, review transition, slot and wake.

    The result-bearing wake is the one way the parent learns the return, so
    a return the parent cannot take now (no model, or nothing left to act on)
    is not harvested at all: the savepoint is rolled back and the node is
    left, whole, for a later sweep. Nothing records an epoch as completed
    without the wake that reports it.
    """
    db.execute("SAVEPOINT harvest_one")
    service._event(
        db, node["id"], "child_plan_completed", actor_id=None,
        provenance="plan.delegation_harvest",
        causal_event_id=None if child_event is None else child_event["id"],
        payload={
            "child_plan_id": payload["child_plan_id"],
            "attachment_event_id": attachment["id"],
        },
    )
    service._transition(
        db, node["id"], "review_requested", actor_id=None,
        provenance="plan.delegation_harvest",
        verification_result=None, verified_by=None,
    )
    _hand_on_parent_slot(node, db)
    accepted = plan_wake.notify_returned(
        db, node_id=node["id"], child_plan_id=payload["child_plan_id"],
        worker_thread_id=payload["spawned_thread_id"], reply=reply,
    )
    if accepted is None:
        db.execute("ROLLBACK TO harvest_one")
    db.execute("RELEASE harvest_one")
    return accepted is not None


def harvest_ready_children(
    *,
    db,
    drives: list[str],
    changed_plan_ids: list[str],
    worker_thread_id: str | None = None,
) -> list[str]:
    """Move freshly terminal child epochs and collect their post-commit effects.

    The periodic recovery sweep omits ``worker_thread_id`` and considers every
    live attachment. The hot Turn-end path supplies the worker that just ended,
    so its indexed direct attachment is the only candidate. Both routes use the
    same readiness and harvest implementation.

    This function only writes and collects. Its caller owns the outer commit,
    then delivers ``changed_plan_ids`` and starts ``drives`` after that commit.
    """
    harvested = []
    changed = []
    with (
        plan_write.transaction(db) as db,
        notifications.collect_wake_recipients(db, drives),
    ):
        live, states = _live_attachment(_HARVESTABLE_STATES)
        worker_filter = "" if worker_thread_id is None else " AND spawned_thread_id=?"
        params = states if worker_thread_id is None else (*states, worker_thread_id)
        rows = db.execute(
            f"""SELECT * FROM plan_nodes
                WHERE spawned_thread_id IS NOT NULL AND {live}{worker_filter}
                ORDER BY id""",
            params,
        ).fetchall()
        for node in rows:
            epoch = _valid_structured_epoch(node, db)
            if epoch is None:
                continue
            attachment, payload = epoch
            if _harvested(node["id"], attachment["id"], db):
                continue
            if not _ready(node, payload, db):
                continue
            child_event = _latest_child_event(payload["child_plan_id"], db)
            if child_event is None:
                continue
            if not _harvest(node, attachment, payload, child_event, db):
                continue
            harvested.append(node["id"])
            changed.append(node["plan_id"])
    for plan_id in changed:
        if plan_id not in changed_plan_ids:
            changed_plan_ids.append(plan_id)
    return harvested
