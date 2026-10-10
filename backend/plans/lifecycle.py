"""Canonical whole-plan pause and resume lifecycle.

Pausing an agent pauses the workers it started, and theirs, all the way down;
resuming it resumes the same ones (pause contract rule 1, #1970). Each plan in
the tree moves as its own recorded transition, so each recomputes what is due
when it resumes, and its event names the plan whose pause carried it
(``cascade_root``). A plan paused on its own is where a cascade stops in both
directions: pausing above it claims nothing beneath it, and resuming above it
does not undo it.

Resuming restores what the pause stopped, not more (#2325). A worker that was
not working when the cascade paused it (no running Turn, no liveness wake
armed; a stalled wake is a budget that ran out) is marked ``quiet``, and resumes quietly: back to active, with no task
announced and no fresh liveness budget, unless something arrived for it while
paused. A long-finished worker under a resumed agent therefore stays finished.

A running Turn is not killed. Its thread is told, at its next safe boundary, to
stop at a safe point; **Stop** remains the way to end a Turn now.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
import json

from automation import plan_liveness
from automation.continuation import triggers
import plans.admission as admission
import plans.errors as errors
import plans.hierarchy as hierarchy
import plans.plan_events as plan_events
import plans.service as service


_TRANSITIONS = {
    ("active", "paused"),
    ("paused", "active"),
}

#: The provenance of a transition a pause or resume above carried here.
CASCADE_PROVENANCE = "plans.pause_cascade"


def transition(
    plan_id: str,
    target_state: str,
    *,
    expected_revision: int,
    actor_id: str | None,
    provenance: str,
    causing_executor: str | None = None,
    db,
) -> dict:
    """Apply one revisioned plan lifecycle transition, its cascade and wake effects.

    ``causing_executor`` is the thread whose own write this is, when one is
    acting: resuming a plan reselects its work, and the thread that resumed it
    already knows; pausing its own plan mid-Turn, it needs no telling to stop.
    """

    plan = service._plan_row(db, plan_id)
    prior_state = plan["state"]
    if plan["revision"] != expected_revision:
        raise errors.revision_conflict(
            "plan", plan_id, expected_revision, plan["revision"],
        )
    if (prior_state, target_state) not in _TRANSITIONS:
        raise service.PlanError(
            f"illegal plan transition: {prior_state} -> {target_state}"
        )
    if plan["owner_kind"] != "thread":
        raise service.PlanError("plan lifecycle requires a thread plan")

    common = {"actor_id": actor_id, "causing_executor": causing_executor, "db": db}
    _move(plan, target_state, provenance=provenance, cascade_root=None, **common)
    carried, quiet_ids = _cascade(plan_id, target_state, db=db)
    for row in carried:
        _move(
            row, target_state, provenance=CASCADE_PROVENANCE,
            cascade_root=plan_id, quiet=str(row["id"]) in quiet_ids, **common,
        )

    projected = service._plan_from_row(service._plan_row(db, plan_id))
    return {
        "plan": asdict(projected),
        "cascaded_plan_ids": [str(row["id"]) for row in carried],
    }


def _move(
    plan, target_state: str, *, actor_id: str | None, provenance: str,
    causing_executor: str | None, cascade_root: str | None, quiet: bool = False,
    db,
) -> None:
    """Move one plan, record why, and apply that plan's own wake effects.

    ``quiet`` resumes a carried plan without its wake effects (module docstring).
    """
    plan_id = str(plan["id"])
    now = service._now()
    cursor = db.execute(
        """UPDATE plans
           SET state=?, revision=revision+1, updated_at=?
           WHERE id=? AND revision=?""",
        (target_state, now, plan_id, plan["revision"]),
    )
    if cursor.rowcount != 1:
        # The initial read is not itself a lock: a concurrent writer could have
        # advanced the revision between that read and this UPDATE. Detect the
        # lost update here rather than appending a transition event for a
        # mutation that never actually applied.
        raise errors.revision_conflict(
            "plan", plan_id, plan["revision"],
            service._plan_row(db, plan_id)["revision"],
        )
    owner_thread = plan["owner_id"]
    payload: dict[str, str | bool] | None = (
        {"cascade_root": cascade_root} if cascade_root else None
    )
    if target_state == "paused":
        admission.release_plan(plan_id, "plan_paused", db=db)
        wake = plan_liveness.wake_for(owner_thread, db)
        armed = wake is not None and wake["status"] == plan_liveness.ARMED
        plan_liveness.cancel_plan_wake(owner_thread, db=db)
        triggers.cancel_for_plan(
            plan_id,
            owner_thread,
            db=db,
            now=now,
        )
        running = owner_thread != causing_executor and _stop_at_safe_point(
            plan_id, owner_thread, int(plan["revision"]) + 1,
            carried=cascade_root is not None, db=db,
        )
        if payload is not None and not (armed or running):
            payload["quiet"] = True
    plan_events.append(
        db, plan_id, "state_transition", actor_id=actor_id, provenance=provenance,
        prior_state=plan["state"], new_state=target_state, payload=payload,
    )
    if target_state == "active" and not quiet:
        admission.reconcile_plan(
            plan_id, actor_id=actor_id, provenance=provenance,
            causing_executor=causing_executor, db=db,
        )
        plan_liveness.on_turn_complete(owner_thread, db=db)


def archive_ended_owner(thread_id: str, *, reason: str, provenance: str, db) -> None:
    """Archive a deleted or purged thread's own plan, in the caller's write (#2647).

    Its owner is gone, so nothing can act on the plan again: its selections
    are released and its liveness wake and continuations cancelled. Settled
    nodes stay as its record. An already archived plan is left alone, so a
    purge after a delete changes nothing.
    """
    plan = db.execute(
        """SELECT id,state FROM plans
           WHERE owner_kind='thread' AND owner_id=? AND state!='archived'""",
        (thread_id,),
    ).fetchone()
    if plan is None:
        return
    plan_id = str(plan["id"])
    now = service._now()
    db.execute(
        "UPDATE plans SET state='archived', revision=revision+1, updated_at=? WHERE id=?",
        (now, plan_id),
    )
    admission.release_plan(plan_id, "plan_archived", db=db)
    plan_liveness.cancel_plan_wake(thread_id, db=db)
    triggers.cancel_for_plan(plan_id, thread_id, db=db, now=now)
    plan_events.append(
        db, plan_id, "state_transition", actor_id=None, provenance=provenance,
        prior_state=plan["state"], new_state="archived", payload={"reason": reason},
    )


def _cascade(plan_id: str, target_state: str, *, db) -> tuple[list, set[str]]:
    """Return the worker plans this transition carries, each parent before its children,
    and the ids among them that resume quietly.

    Pausing carries every running plan below and stops at one already paused,
    whose own pause already holds what is beneath it. Resuming carries every
    plan a cascade paused and passes through running ones, since a worker
    resumed on its own may still have cascade-paused workers; it stops at a
    plan paused on its own.
    """
    below = [
        row for row in hierarchy.descendants(db, [plan_id])
        if row["id"] != plan_id and row["owner_kind"] == "thread"
    ]
    children = defaultdict(list)
    for row in below:
        children[row["parent_plan_id"]].append(row)
    carried_before = (
        _cascade_paused(db, [row["id"] for row in below if row["state"] == "paused"])
        if target_state == "active" else {}
    )
    carried: list = []
    frontier = [plan_id]
    while frontier:
        parent = frontier.pop(0)
        for row in children.get(parent, ()):
            if row["state"] == "active" and target_state == "paused":
                carried.append(row)
            elif row["state"] == "paused" and row["id"] in carried_before:
                carried.append(row)
            elif row["state"] != "active" or target_state == "paused":
                continue
            frontier.append(row["id"])
    return carried, {plan for plan, quiet in carried_before.items() if quiet}


def _cascade_paused(db, plan_ids: list[str]) -> dict[str, bool]:
    """Map the plans among these whose latest pause a cascade carried to quietness.

    Quiet means the worker was not working when paused and nothing was set
    aside for it since. A pause recorded before ``quiet`` existed is not quiet,
    so it resumes the way it always did.
    """
    from plans.pause_authority import held_since

    carried: dict[str, bool] = {}
    quiet_since: dict[str, str] = {}
    for start in range(0, len(plan_ids), hierarchy._ID_CHUNK):
        chunk = plan_ids[start:start + hierarchy._ID_CHUNK]
        marks = ",".join("?" for _ in chunk)
        for row in db.execute(
            f"""SELECT e.plan_id, e.payload_json, e.created_at FROM plan_events e
                WHERE e.rowid IN (
                  SELECT MAX(rowid) FROM plan_events
                  WHERE plan_id IN ({marks}) AND event='state_transition'
                    AND new_state='paused'
                  GROUP BY plan_id
                )""",
            chunk,
        ).fetchall():
            payload = json.loads(row["payload_json"] or "{}")
            if payload.get("cascade_root"):
                carried[str(row["plan_id"])] = bool(payload.get("quiet"))
                if payload.get("quiet"):
                    quiet_since[str(row["plan_id"])] = str(row["created_at"])
        for plan_id in held_since(db, quiet_since):
            carried[plan_id] = False
        quiet_since.clear()
    return carried


def _stop_at_safe_point(
    plan_id: str, owner_thread: str, revision: int, *, carried: bool, db,
) -> bool:
    """Tell a thread whose Turn is running that its plan is paused; True if one runs.

    Only a running Turn is told: the input's currency (``wake_currency``) is a
    Turn to hear it, so it reaches that Turn at a safe boundary and can never
    start one.
    """
    from automation.continuation.models import StaleContinuation
    from plans import plan_wake, wake_currency
    from prompts import plan_wakes as text

    running = db.execute(
        "SELECT 1 FROM active_turns WHERE thread_id=? AND status='running'",
        (owner_thread,),
    ).fetchone()
    if running is None:
        return False
    try:
        plan_wake.append_input(
            db,
            recipient=owner_thread,
            message_id=f"plan-wake:paused:{plan_id}:{revision}",
            content=text.WAKE_PAUSED.format(
                how=text.PAUSED_WITH_PARENT if carried else "",
            ),
            metadata={"event_type": "plan.paused", "origin": "plan", "plan_id": plan_id},
            continuation=wake_currency.pause_condition(
                plan_id=plan_id, revision=revision,
            ),
            skip_when_model_less=True,
        )
    except StaleContinuation:
        pass
    return True
