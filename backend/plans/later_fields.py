"""The Later cut on one task: its refusals, and the write every flip makes.

An agent puts work it will not do now below the cut with `later`. The flag is
ranking, not lifecycle: `state` keeps its seven values, and only tasks carry it.
A branch is Later when every open task beneath it is, which the server rolls up
from the tasks, so a stored flag on a branch would be a second answer to one
question.

The reason rides the event and never a column, following the precedent of the
scheduled reason that rides `watch_armed` (`plans.scheduled_wait`): the row shows
the latest one, and a flip that changes nothing writes nothing.
"""

from __future__ import annotations

import sqlite3

from plans import service

#: The one event every flip writes, with ``{"later": bool, "reason": str|null}``.
EVENT = "node_later"

#: Shown when the cut is asked for on a branch. It names the alternative rather
#: than only refusing: the caller can pass the branch's open task ids in one call.
LATER_ON_BRANCH = (
    "Later applies to tasks. To put this branch in Later, pass its open task ids"
    " in node_ids."
)
#: A reason explains a Later row; on its own it explains nothing.
REASON_WITHOUT_LATER = "reason goes with later: it is why this task is in Later"


def validate_flag(value: object) -> bool:
    """Return the flag as a bool, refusing anything that is not one."""
    if not isinstance(value, bool):
        raise service.PlanInputError("later must be true or false")
    return value


def validate_carries_later(kind: str) -> None:
    """Refuse the flag on a phase or track, whose Later is derived instead."""
    if kind != "task":
        raise service.PlanInputError(LATER_ON_BRANCH)


def creation_flag(kind: str, requested: object) -> bool:
    """Return the flag a new node is born with.

    A birth below the cut is not a crossing: there is no earlier state to
    explain, so it asks no reason and writes no event. Only a task carries it.
    """
    flag = validate_flag(requested)
    if flag:
        validate_carries_later(kind)
    return flag


def edit_fields(params: dict, text) -> dict:
    """Return an edit's Later fields: the flag and the reason that rides it.

    The reason rides the flag and nothing else, so an edit naming ``reason``
    without ``later`` is refused at the boundary (``REASON_WITHOUT_LATER``).
    """
    return {
        "later": validate_flag(params["later"]),
        "reason": None if params.get("reason") is None else text(params, "reason"),
    }


def record_event(
    connection: sqlite3.Connection, node_id: str, *, later: bool, reason: str | None,
    actor_id: str | None, provenance: str,
) -> None:
    """Record one crossing of the cut, with the reason the caller gave.

    An event with ``reason: null`` still stands: the move itself is the record,
    and only the latest reason is shown on the row.
    """
    service._event(
        connection, node_id, EVENT, actor_id=actor_id, provenance=provenance,
        payload={"later": later, "reason": reason},
    )


def edit_across_cut(params: dict, changed: dict, common: dict, acting: str | None) -> dict:
    """Apply an edit that names the cut, and free the slot when the cut moved onto it.

    Moving the task that holds the executor's slot below the cut is a park: the
    attempt is released, the plan reconciles, and the answer carries the same
    ``next`` block a park returns, so an agent never holds a slot its own edit
    moved out from under it (Later design §2). Any other answer is the node and
    its admission truth, as before an edit could name the cut.

    An edit that also changes the task's requirements has already released the
    attempt itself, and does not reselect a Later task, so the slot counts as
    freed whichever release ran.
    """
    from plans import admission, close_next
    from plans import mutations
    from plans.boundary_fields import expected

    import plans.admission_attempts as attempts

    node_id, db = params["node_id"], common["db"]
    before = service._node_row(db, node_id)
    slot = close_next.slot_before(db, before)
    held = attempts.open_attempt(db, node_id) is not None
    mutations.edit_node(node_id, **changed, **expected(params), **common)
    if changed["later"] and held:
        admission.release(node_id, "later", db=db)
    if not changed["later"] or not held or attempts.open_attempt(db, node_id) is not None:
        return admission.result(node_id, db=db)
    admission.reconcile_plan(
        slot.plan_id, actor_id=common["actor_id"], provenance=common["provenance"],
        causing_executor=acting, db=db,
    )
    result = admission.result(node_id, db=db)
    # The parked task stays open, so it is no closed task to leave out: it counts as Later.
    result.update(close_next.next_fields(db, None, slot, None, acting=acting))
    return result


def flip(
    connection: sqlite3.Connection, node_id: str, *, later: bool,
    reason: str | None, actor_id: str | None, provenance: str,
) -> None:
    """Move one task across the cut and record why, in one transaction.

    ``touch_node`` stays the only writer of the row and its revision. An edit
    that also restates the node writes its own column through ``touch_node`` and
    calls ``record_event``, so one edit costs one revision bump, never two.
    """
    service.touch_node(connection, node_id, later=int(later))
    record_event(
        connection, node_id, later=later, reason=reason,
        actor_id=actor_id, provenance=provenance,
    )
