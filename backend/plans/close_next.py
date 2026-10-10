"""What a closing or parking plan mutation reports as next, and the task it may start.

A closing or parking operation (``boundary_fields.NEXT_OPERATIONS``) takes its
executor off one task, ending that work or setting it aside. Its result carries a bounded ``next`` block: the
task now holding that executor's slot in the plan and how it got there, a few
of the executor's other open tasks, and how many there are. A caller may name
the task to start next; nothing pending is ever started unasked.

This module is composition only. The named start goes through
``admission.activate`` or the selection ``admission.start`` makes; whether the
boundary may drive the named node is ``boundary_guard``'s answer; why a task
cannot take its slot is ``selection_validity``'s; occupancy, open tasks and
their order are read from their owners; and every word an agent reads comes
from ``prompts.plan_tools``. It selects nothing itself and owns no state.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import plans.admission as admission
import plans.admission_attempts as attempts
import plans.boundary_guard as boundary_guard
import plans.close_next_idle as close_next_idle
import plans.handoff_brief as handoff_brief
import plans.node_order as node_order
import plans.node_settlement as node_settlement
import plans.returned_feedback as returned_feedback
import plans.selection_validity as validity
import plans.service as service
import plans.triage_lines as triage_lines
from plans.close_next_bounds import (
    CRITERIA_MIN_BYTES,
    NEXT_BLOCK_MAX_BYTES,
    REASON_MAX_BYTES,
    TITLE_MAX_BYTES,
    WAIT_MAX_BYTES,
    CloseNextInvariantError,
    bounded,
    fit,
    json_bytes,
    slot_held_reason,
    FEEDBACK_MAX_BYTES,
)
from prompts import plan_tools as text

#: The executor's other open tasks one block lists.
NEXT_OPEN_TASK_LIMIT = 3

#: ``named.refusal`` codes: why the named task did not start.
UNKNOWN = "unknown"
NOT_OPEN = "not_open"
LANE_OWNED = "lane_owned"
USER_ASSIGNED = "user_assigned"
OTHER_EXECUTOR = "other_executor"
NOT_READY = "not_ready"
SLOT_HELD = "slot_held"

#: ``selected.how``: how the task holding the slot came to hold it.
STARTED = "started"
AUTO_SELECTED = "auto_selected"
RUNNING = "running"

_HELD_CONDITIONS = close_next_idle.HELD_CONDITIONS
_CRITERIA_HOWS = frozenset({STARTED, AUTO_SELECTED})
_LANE_REASONS = {node_settlement.DECISION: text.DECISION_OWNED}


@dataclass(frozen=True)
class SlotSnapshot:
    """The plan and executor whose slot a close frees, and its holder before release."""

    plan_id: str
    executor: str
    attempt_id: str | None


@dataclass(frozen=True)
class Listing:
    """The open-task rows one block lists, their count, label, and idle line."""

    rows: list[dict]
    total: int
    label: Callable[[int, int], str]
    idle: str | None = None


@dataclass(frozen=True)
class NamedStart:
    """The named next task, and why it did not start, judged before reconciliation."""

    node_id: str | None
    refusal: str | None = None
    reason: str | None = None
    #: The triage line for a start that passed ready work (``plans.triage_lines``).
    jumped: str | None = None


def slot_before(db, closed) -> SlotSnapshot:
    """Capture the closed task's executor and slot holder before its release."""
    plan = service._plan_row(db, closed["plan_id"])
    executor = admission.resolve_executor(db, closed, plan)
    holder = attempts.slot_holder(db, plan["id"], executor)
    return SlotSnapshot(
        str(plan["id"]), executor, None if holder is None else str(holder["id"]),
    )


def start_named(
    db, node_id: str, slot: SlotSnapshot, *, actor_id: str | None, provenance: str,
) -> NamedStart:
    """Start the named task as activate or start would, and judge the outcome.

    Runs after the close released its task and before the plan reconciles. A
    refusal found before any write leaves the named task untouched. Otherwise a
    pending task goes through ``admission.activate`` and an active one through
    ``admission.start_selection``, with their own outcomes: an occupied slot
    leaves a pending task pending, and an activated task that is not eligible
    yet stays active and queued for reconciliation.
    """
    node = db.execute("SELECT * FROM plan_nodes WHERE id=?", (node_id,)).fetchone()
    if node is None:
        return NamedStart(None, UNKNOWN, text.UNKNOWN_TASK)
    plan = service._plan_row(db, slot.plan_id)
    refused = _refused_before_write(db, node, plan, slot.executor)
    if refused is not None:
        return refused
    common: dict[str, Any] = {"actor_id": actor_id, "provenance": provenance, "db": db}
    jump = triage_lines.jump_for_start(db, node, actor_id=actor_id)
    if node["state"] == "pending":
        admission.activate(
            node_id, expected_revision=node["revision"], **common,
            payload=None if jump is None else jump.payload,
        )
    else:
        admission.start_selection(node_id, **common)
    holder = attempts.slot_holder(db, slot.plan_id, slot.executor)
    if _holds_validly(db, holder, node_id):
        return NamedStart(node_id, jumped=None if jump is None else jump.line)
    refusal, reason = _not_started(db, node_id, plan, holder)
    return NamedStart(node_id, refusal, reason)


def executor_next(db, plan_id: str, executor: str) -> dict:
    """Compose the same bounded block for one executor's slot, with no close.

    A wake tells a thread what a close would have told it: what holds its slot
    in this plan now, and which of its other open tasks are waiting. The close
    path excludes the task it just ended; here there is none to exclude.
    """
    return next_block(db, None, SlotSnapshot(plan_id, executor, None), None)


def next_block(db, closed, slot: SlotSnapshot, named: NamedStart | None) -> dict:
    """Compose the bounded ``next`` block once the close has reconciled its plan."""
    plan = service._plan_row(db, slot.plan_id)
    holder = attempts.slot_holder(db, slot.plan_id, slot.executor)
    started = named is not None and _holds_validly(db, holder, named.node_id)
    listing = _open_rows(db, closed, plan, slot.executor, holder)
    selected = _selected(db, holder, slot, started)
    block: dict[str, Any] = {
        "executor_thread_id": slot.executor,
        "open_tasks": listing.rows,
        "open_tasks_listed": listing.label(len(listing.rows), listing.total),
        "selected": selected,
    }
    if listing.idle is not None:
        block["idle"] = listing.idle
    if named is not None:
        block["named"] = _named(named, started)
    criteria = None
    if selected is not None and selected["how"] in _CRITERIA_HOWS:
        criteria = str(service._node_row(db, holder["node_id"])["acceptance_criteria"])
    return fit(block, listing.total, criteria, listing.label)


def next_fields(
    db, closed, slot: SlotSnapshot, named: NamedStart | None, *, acting: str | None,
) -> dict[str, Any]:
    """Return what a close or park adds to its result: ``next`` and what rides beside it.

    A task the write just handed its executor brings its description as a
    sibling of the block (``handoff_brief``), outside the block's byte cap.
    Only when that executor is ``acting``, the thread this result returns to:
    someone else's close (a reviewer's accept) can fill the executor's slot
    too, and the description then reaches the executor in its work-selected
    wake, never the thread that closed.
    """
    block = next_block(db, closed, slot, named)
    selected = block["selected"]
    if selected is None or selected["how"] not in _CRITERIA_HOWS or slot.executor != acting:
        return {"next": block}
    return {"next": block, **handoff_brief.fact(db, selected["id"], slot.executor)}


def _refused_before_write(db, node, plan, executor: str) -> NamedStart | None:
    """Return why this node may not start for the executor, before any write, or None.

    The user's work and another executor's work are refused here, never written."""
    operation = "activate" if node["state"] == "pending" else "start"
    guard = boundary_guard.drive_refusal(
        db, node, plan=plan, operation=operation, executor=executor,
    )
    code = None if guard is None else guard.code
    if code == boundary_guard.OUTSIDE_PLAN:
        # One answer for missing and elsewhere, so another plan's node stays undisclosed.
        return NamedStart(None, UNKNOWN, text.UNKNOWN_TASK)
    if code == boundary_guard.ARCHIVED:
        return NamedStart(None, NOT_OPEN, text.ARCHIVED_TASK)
    if node["kind"] != "task":
        return NamedStart(None, NOT_OPEN, text.NODE_KIND.format(kind=node["kind"]))
    if node["state"] not in node_order.OPEN_TASK_STATES:
        return NamedStart(None, NOT_OPEN, text.TASK_STATE.format(state=node["state"]))
    if guard is not None and code == boundary_guard.LANE_OWNED:
        return NamedStart(None, LANE_OWNED, _LANE_REASONS[guard.lane])
    if validity.row_blocker(node, plan) == validity.USER_ASSIGNED:
        return NamedStart(None, USER_ASSIGNED, text.USER_ASSIGNED)
    if code == boundary_guard.OTHER_EXECUTOR:
        return NamedStart(None, OTHER_EXECUTOR, text.OTHER_EXECUTOR)
    return None


def _not_started(db, node_id: str, plan, holder) -> tuple[str, str]:
    """Explain from the written state why an offered task holds no slot.

    The eligibility owner names the first rule the task fails, in report order;
    only an eligible task is kept out by the task holding the slot.

    Raises:
        CloseNextInvariantError: Nothing explains it, so selection diverged
            from the eligibility rule it asks.
    """
    node = service._node_row(db, node_id)
    blocker = validity.slot_blocker(db, node, plan)
    if blocker == validity.USER_ASSIGNED:
        return USER_ASSIGNED, text.USER_ASSIGNED
    if blocker is not None:
        return NOT_READY, _not_ready_reason(db, node_id, plan, blocker)
    if holder is not None:
        return SLOT_HELD, slot_held_reason(node["state"], holder)
    raise CloseNextInvariantError("next task selection diverged from its eligibility check")


def _not_ready_reason(db, node_id: str, plan, blocker: str) -> str:
    """Return why an offered task is not ready, as the plan shows it.

    A held task, one in a paused plan included, reads as its projected
    condition and reason.

    Raises:
        CloseNextInvariantError: No reason reads for this blocker.
    """
    if blocker == validity.HAS_CHILDREN:
        return text.HAS_CHILDREN
    projection = admission.project(node_id, db=db)
    if projection["condition"] in _HELD_CONDITIONS:
        return text.condition_text(projection["condition"], projection["primary_reason"])
    if blocker == validity.PLAN_INACTIVE:
        return text.PLAN_STATE.format(state=plan["state"])
    raise CloseNextInvariantError(f"an offered task has no reportable {blocker} reason")


def _named(named: NamedStart, started: bool) -> dict[str, Any]:
    """Return the ``named`` section once reconciliation has run.

    Raises:
        CloseNextInvariantError: Reconciliation displaced a task the named
            start had selected; it never preempts a valid selection.
    """
    if started:
        reply: dict[str, Any] = {"reason": None, "refusal": None, "started": True}
        if named.jumped is not None:
            reply["jumped"] = named.jumped
        return reply
    if named.refusal is None or named.reason is None:
        raise CloseNextInvariantError("reconciliation displaced the named next task")
    return {
        "reason": bounded(named.reason, REASON_MAX_BYTES),
        "refusal": named.refusal,
        "started": False,
    }


def _selected(db, holder, slot: SlotSnapshot, started: bool) -> dict[str, Any] | None:
    """Return the task holding the executor's slot and how it got there."""
    if holder is None:
        return None
    if started:
        how = STARTED
    elif str(holder["id"]) == slot.attempt_id:
        how = RUNNING
    else:
        how = AUTO_SELECTED
    selected = _task_row(
        holder["node_id"], holder["revision"], holder["state"], holder["title"], None,
    )
    selected["how"] = how
    standing = returned_feedback.standing_feedback(db, str(holder["node_id"]))
    if standing is not None and not standing.readable:
        # The revision's recorded feedback is corrupt: say so rather than hand
        # the executor a revision with its reason silently missing.
        selected["feedback_unreadable"] = True
    elif standing is not None:
        selected["feedback"] = text.bounded_text(
            standing.feedback, FEEDBACK_MAX_BYTES, measure=json_bytes, marker=text.FEEDBACK_CUT,
        )
    return selected


def _open_rows(db, closed, plan, executor: str, holder) -> Listing:
    """Return the listed open-task rows, how many open tasks there are, and their label.

    The count and the rows come from one ordered list that already excludes the
    closed task and the slot holder, so ``k of N`` describes the list it labels.
    An executor with nothing in its slot and nothing ready gets the idle
    listing instead (``plans.close_next_idle``).
    """
    everything = node_order.open_tasks(db, plan, executor)
    if holder is None:
        now = datetime.fromisoformat(service._now())
        idle = close_next_idle.idle_listing(db, plan, everything, now)
        if idle is not None:
            return _idle_rows(idle)
    # A wake composes this block with no closed task to leave out.
    excluded = set() if closed is None else {str(closed["id"])}
    if holder is not None:
        excluded.add(str(holder["node_id"]))
    tasks = [row for row in everything if str(row["id"]) not in excluded]
    shown = tasks[:NEXT_OPEN_TASK_LIMIT]
    projections = admission.project_many([(row, plan) for row in shown], db=db)
    rows = [
        _task_row(
            row["id"], row["revision"], row["state"], row["title"],
            _wait(projections[str(row["id"])]),
        )
        for row in shown
    ]
    return Listing(rows, len(tasks), text.listed_text)


def _idle_rows(idle: close_next_idle.IdleListing) -> Listing:
    """Return an idle executor's listing: each row with when its wait ends, or None.

    Only the idle listing carries ``ends``: it has no selected task, so the
    bytes the criteria would take are free for it.
    """
    rows = []
    for row in idle.shown:
        node_id = str(row["id"])
        listed = _task_row(
            row["id"], row["revision"], row["state"], row["title"],
            _wait(idle.projections[node_id]),
        )
        listed.update(close_next_idle.release_wait(idle.projections[node_id], row))
        seconds = idle.seconds[node_id]
        listed["ends"] = None if seconds is None else text.ends_text(seconds)
        rows.append(listed)
    pick_up = text.IDLE_PICK_UP if idle.pick_up else None
    return Listing(rows, idle.total, text.idle_listed_text, pick_up)


def _wait(projection: dict) -> str | None:
    """Return a listed task's held condition and reason, or None when it is not held."""
    if projection["condition"] not in _HELD_CONDITIONS:
        return None
    return bounded(
        text.condition_text(projection["condition"], projection["primary_reason"]),
        WAIT_MAX_BYTES,
    )


def _task_row(node_id, revision, state, title, wait: str | None) -> dict[str, Any]:
    """Return one task row; its id, revision and state are exact."""
    return {
        "id": str(node_id),
        "revision": int(revision),
        "state": str(state),
        "title": bounded(str(title), TITLE_MAX_BYTES),
        "wait": wait,
    }


def _holds_validly(db, holder, node_id: str | None) -> bool:
    """Return whether the slot holder is this task's valid selection."""
    return (
        holder is not None
        and node_id is not None
        and str(holder["node_id"]) == node_id
        and validity.selection_valid(db, holder)
    )

