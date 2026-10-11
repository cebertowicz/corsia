"""Canonical executor selection and effective-condition projection."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass

import plans.admission_attempts as attempts
import plans.errors as errors
import plans.node_states as node_states
import plans.selection_validity as validity
import plans.service as service

# Projection is split out to keep admission's mutation owner within its file budget.


def resolve_executor(db, node, plan=None) -> str:
    """Resolve the one execution assignment without changing accountable ownership."""

    plan = plan or service._plan_row(db, node["plan_id"])
    executor = _projection().executor_of(node, plan)
    if executor is None:
        raise service.PlanError("task has no executable thread assignment")
    return executor


def _projection():
    from plans import admission_projection

    return admission_projection


def project_many(node_plans, *, db, memo=None) -> dict[str, dict]:
    """Project many nodes through one bounded-query canonical path."""

    return _projection().project_many(node_plans, db=db, memo=memo)


def project(node_id: str, *, db, conflict: dict | None = None) -> dict:
    """Return the compact canonical admission projection for one node."""

    return _projection().project(node_id, db=db, conflict=conflict)


def result(node_id: str, *, db, conflict: dict | None = None) -> dict:
    """Return one node and its admission truth from the same transaction."""

    return {
        "node": asdict(service.get_node(node_id, db=db)),
        "admission": project(node_id, db=db, conflict=conflict),
    }


def selection_conflict(db, node) -> dict | None:
    """Return the task occupying this node's executor slot, if any."""
    executor = resolve_executor(db, node)
    return attempts.selection_conflict(db, node, executor)


def select(
    node_id: str,
    *,
    actor_id: str | None,
    provenance: str,
    db,
    touch: bool = True,
) -> tuple[sqlite3.Row | None, dict | None]:
    """Open one attempt if the task and executor are currently admissible."""

    node = service._live_node_row(db, node_id)
    plan = service._plan_row(db, node["plan_id"])
    existing = attempts.open_attempt(db, node_id)
    if existing is not None:
        if validity.selection_valid(db, existing):
            return existing, None
        if not release(node_id, "selection_invalidated", db=db):
            # An already-started call defers this release; the node cannot
            # hold a second open attempt until that call settles.
            return None, None
    if not validity.eligible(db, node, plan):
        return None, None
    executor = resolve_executor(db, node, plan)
    conflict = selection_conflict(db, node)
    if conflict is not None:
        return None, conflict
    try:
        attempt = attempts.insert(
            db, node, executor, actor_id=actor_id, provenance=provenance,
        )
    except sqlite3.IntegrityError:
        return None, selection_conflict(db, node)
    attempts.bind_turn(db, node, attempt, executor)
    if touch:
        service.touch_node(db, node_id)
    return attempt, None


def release(node_id: str, reason: str, *, db) -> bool:
    """Expose the safe attempt-release operation owned by admission."""
    return attempts.release(node_id, reason, db=db)


def release_plan(plan_id: str, reason: str, *, db) -> None:
    """Safely release every selected task in one plan."""
    rows = db.execute(
        "SELECT node_id FROM plan_node_attempts WHERE plan_id=? AND released_at IS NULL",
        (plan_id,),
    ).fetchall()
    for row in rows:
        release(row["node_id"], reason, db=db)


@dataclass(frozen=True)
class Reconciled:
    """What one reconciliation changed: the work it released, and what it started.

    A record of the change, not an instruction to notify anybody.
    ``reconcile_plan`` tells the newly selected executors itself, because it is
    the one place that knows what it selected; leaving that to each caller made
    delivery depend on which mutation path produced the same plan state, and
    twelve of fifteen callers discarded this value and told nobody. Callers
    read it to report the change onward, never to decide the audience.
    """

    released: tuple[tuple[str, str], ...] = ()
    selected: tuple[tuple[str, str], ...] = ()
    selection_woken: frozenset[str] = frozenset()


def reconcile_plan(
    plan_id: str,
    *,
    actor_id: str | None,
    provenance: str,
    causing_executor: str | None = None,
    candidate_ids: tuple[str, ...] | None = None,
    selection_wake_summaries: dict[str, str] | None = None,
    from_worker: bool = False,
    db,
) -> Reconciled:
    """Release invalid selections, then fill empty executor slots in plan order.

    Every eligibility change calls this inside its own transaction, so the
    next eligible task is selected with no `start`, next Turn or sweep. A
    valid selection is never preempted; a release still deferred by an
    in-flight call keeps its slot until that call settles. Thread-owned plans
    batch eligibility and offer only their canonically first eligible task,
    because every task has the same sole executor; multi-executor scope plans
    retain their full scan. ``candidate_ids`` may carry an already-read ordered
    candidate set; ``select`` still enforces every eligibility predicate.

    Telling the newly selected executors happens here, with the selection, so
    every writer that changes eligibility delivers the same news. The one fact
    a caller knows and this does not is ``causing_executor``: the thread whose
    own write this is, which is never told, because its own result already
    carries these facts.

    A worker plan's reconciliation then reconciles its delegator's plan
    (``delegated_selection``). ``from_worker`` marks that one: it goes no
    further up, and a delegated step it selects is the matrix's news.
    """
    from plans import node_order

    open_rows = db.execute(
        """SELECT * FROM plan_node_attempts
           WHERE plan_id=? AND released_at IS NULL ORDER BY selected_at,id""",
        (plan_id,),
    ).fetchall()
    valid_ids = {row["id"] for row in validity.valid_selections(db, open_rows)}
    # Only an attempt this reconciliation opened is news to anyone: a selection
    # that was already valid belongs to an executor already working on it.
    held_before = {str(row["id"]) for row in open_rows}
    released: list[tuple[str, str]] = []
    for attempt in open_rows:
        if attempt["id"] not in valid_ids and release(
            attempt["node_id"], "selection_invalidated", db=db,
        ):
            released.append(
                (str(attempt["node_id"]), str(attempt["executor_thread_id"])),
            )
    selected: list[tuple[str, str]] = []
    plan = service._plan_row(db, plan_id)
    candidates = candidate_ids
    if candidates is None and plan["owner_kind"] == "thread":
        first_eligible_id = validity.first_eligible_active_id(db, plan)
        candidates = (
            (first_eligible_id,) if first_eligible_id is not None else ()
        )
    elif candidates is None:
        candidates = tuple(node_order.active_task_ids(db, plan_id))
    for node_id in candidates:
        attempt, _conflict = select(
            node_id, actor_id=actor_id, provenance=provenance,
            db=db, touch=False,
        )
        if attempt is not None and str(attempt["id"]) not in held_before:
            selected.append((node_id, str(attempt["executor_thread_id"])))
    reconciled = Reconciled(tuple(released), tuple(selected))
    reconciled = _with_selection_wakes(
        db, reconciled, plan_id=plan_id, causing_executor=causing_executor,
        selection_wake_summaries=selection_wake_summaries, from_worker=from_worker,
    )
    if not from_worker and plan["parent_plan_id"] is not None:
        from plans import delegated_selection

        delegated_selection.reconcile_delegator(plan_id, db=db)
    return reconciled


def _with_selection_wakes(
    db, reconciled: Reconciled, *, plan_id: str, causing_executor: str | None,
    selection_wake_summaries: dict[str, str] | None, from_worker: bool,
) -> Reconciled:
    """Write selection wakes and retain the nodes whose wake was accepted."""
    from plans import selection_wake

    selection_woken = selection_wake.notify(
        db, reconciled, plan_id=plan_id, causing_executor=causing_executor,
        selection_wake_summaries=selection_wake_summaries, from_worker=from_worker,
    )
    return Reconciled(
        reconciled.released, reconciled.selected, frozenset(selection_woken),
    )


def reselect_and_reconcile(
    node_id: str,
    *,
    actor_id: str | None,
    provenance: str,
    causing_executor: str | None = None,
    db,
) -> Reconciled:
    """Offer one task its executor's slot first, then reconcile its plan.

    A requirements edit supersedes a task's attempt without making the task
    ineligible. The writer that released that attempt calls this: the task
    takes its slot back at its current requirements and executor before plan
    order fills any other empty slot, so continuing work is never handed to an
    earlier queued task. `select` leaves an ineligible task or an occupied
    slot unchanged. A retired (archived or closed) task cannot be selected, so
    its plan is only reconciled: settling a call left open on a task closed
    since then must not refuse the terminal write it runs inside.
    """
    node = service._node_row(db, node_id)
    if not node_states.retired(node):
        select(node_id, actor_id=actor_id, provenance=provenance, db=db, touch=False)
    return reconcile_plan(
        node["plan_id"], actor_id=actor_id, provenance=provenance,
        causing_executor=causing_executor, db=db,
    )


def make_current(
    node_id: str,
    *,
    expected_revision: int,
    actor_id: str | None,
    provenance: str,
    db,
    payload: dict | None = None,
) -> dict:
    """Make one node the caller's current work, whichever verb was used.

    `activate` and `start` are the same intent -- make this my current task --
    and which one applies is a fact about the node, not about the caller: a
    pending node is activated, an active task is started. Requiring the caller
    to know that only produces a refusal naming the other verb, so the node's
    state chooses here and either verb succeeds. The owner's own task up for
    review is taken back the same way: it returns to active and is started,
    with nothing sent back or failed. A delegated step under review is the
    worker's return, and settled work has no way back; each refusal names the
    route that applies instead.
    """

    node = service._live_node_row(db, node_id)
    errors.require_node_revision(node, expected_revision)
    if node["state"] == "pending":
        return activate(
            node_id, expected_revision=expected_revision, actor_id=actor_id,
            provenance=provenance, db=db, payload=payload,
        )
    if node["state"] == "review_requested" and node["kind"] == "task":
        if node["spawned_thread_id"]:
            raise service.PlanError(
                f"{node_id} is a delegated step under review: accept it, "
                "or send it back with reject"
            )
        service._transition(
            db, node_id, "active", actor_id=actor_id, provenance=provenance,
            verification_result=None, verified_by=None,
        )
        node = service._node_row(db, node_id)
        expected_revision = node["revision"]
    if node["state"] == "active" and node["kind"] == "task":
        return start(
            node_id, expected_revision=expected_revision, actor_id=actor_id,
            provenance=provenance, db=db,
        )
    if node["state"] in node_states._TERMINAL_STATES:
        raise service.PlanError(
            f"{node_id} is {node['state']} and cannot reopen; "
            "to do it again, add_follow_up or add a new task"
        )
    raise service.PlanError(
        "activate and start need a pending node or an active task; "
        f"{node_id} is {node['state']}"
        + ("" if node["kind"] == "task" else f" and is a {node['kind']}")
    )


def activate(
    node_id: str,
    *,
    expected_revision: int,
    actor_id: str | None,
    provenance: str,
    db,
    payload: dict | None = None,
) -> dict:
    """Activate pending work only when its executor slot can be acquired.

    ``payload`` rides the task's activation event: the one durable fact a
    caller wants recorded with the start (``plans.triage_lines``).
    """

    node = service._live_node_row(db, node_id)
    errors.require_node_revision(node, expected_revision)
    if node["state"] != "pending":
        raise service.PlanError("activate requires a pending node")
    if node["kind"] != "task":
        service._transition(
            db, node_id, "active", actor_id=actor_id, provenance=provenance,
            verification_result=None, verified_by=None,
        )
        return result(node_id, db=db)
    conflict = selection_conflict(db, node)
    if conflict is not None:
        return result(node_id, db=db, conflict=conflict)
    service._transition(
        db, node_id, "active", actor_id=actor_id, provenance=provenance,
        verification_result=None, verified_by=None, payload=payload,
    )
    _attempt, conflict = select(
        node_id, actor_id=actor_id, provenance=provenance, db=db, touch=False,
    )
    return result(node_id, db=db, conflict=conflict)


def start(
    node_id: str,
    *,
    expected_revision: int,
    actor_id: str | None,
    provenance: str,
    db,
) -> dict:
    """Select one active ready task without restating its lifecycle.

    `select` alone answers: a task keeps its valid selection, and one that
    neither holds nor can open a selection - it is not eligible, or its
    superseded attempt still waits for an already-started call - is not ready
    to start. An occupied slot is returned as the conflict.
    """

    node = service._live_node_row(db, node_id)
    errors.require_node_revision(node, expected_revision)
    if node["state"] != "active":
        raise service.PlanError("start requires an active task")
    conflict, ready = start_selection(
        node_id, actor_id=actor_id, provenance=provenance, db=db,
    )
    if not ready:
        raise service.PlanError("task is not ready to start")
    return result(node_id, db=db, conflict=conflict)


def start_selection(
    node_id: str, *, actor_id: str | None, provenance: str, db,
) -> tuple[dict | None, bool]:
    """Perform the selection ``start`` makes, and say whether the task was ready.

    ``select`` alone answers: the task keeps or opens a valid selection, or
    another task holds the slot (the conflict). Returns the conflict, if any,
    and False when neither holds - the task is not eligible, or its superseded
    attempt still waits for an already-started call. ``start`` refuses that
    case; a closing operation's named next task reports it.
    """
    attempt, conflict = select(
        node_id, actor_id=actor_id, provenance=provenance, db=db,
    )
    return conflict, attempt is not None or conflict is not None
