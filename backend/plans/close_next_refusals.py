"""Why a task named or offered as next did not start: the ``named.refusal`` codes and their reasons.

Split from ``plans.close_next``, which composes the ``next`` block; every word
comes from ``prompts.plan_tools``.
"""

from __future__ import annotations

from dataclasses import dataclass

import plans.admission as admission
import plans.boundary_guard as boundary_guard
import plans.close_next_idle as close_next_idle
import plans.node_order as node_order
import plans.node_settlement as node_settlement
import plans.selection_validity as validity
import plans.service as service
from plans.close_next_bounds import CloseNextInvariantError, slot_held_reason
from prompts import plan_tools as text

#: ``named.refusal`` codes: why the named task did not start.
UNKNOWN = "unknown"
NOT_OPEN = "not_open"
LANE_OWNED = "lane_owned"
USER_ASSIGNED = "user_assigned"
OTHER_EXECUTOR = "other_executor"
NOT_READY = "not_ready"
SLOT_HELD = "slot_held"

_HELD_CONDITIONS = close_next_idle.HELD_CONDITIONS
_LANE_REASONS = {node_settlement.DECISION: text.DECISION_OWNED}


@dataclass(frozen=True)
class NamedStart:
    """The named next task, and why it did not start, judged before reconciliation."""

    node_id: str | None
    refusal: str | None = None
    reason: str | None = None
    #: The triage line for a start that passed ready work (``plans.triage_lines``).
    jumped: str | None = None


def refused_before_write(db, node, plan, executor: str) -> NamedStart | None:
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
        from plans import workflow_fields

        if workflow_fields.person_gate(db, node["id"]):
            return NamedStart(None, USER_ASSIGNED, text.PERSON_GATE_NAMED)
        return NamedStart(None, USER_ASSIGNED, text.USER_ASSIGNED)
    if code == boundary_guard.OTHER_EXECUTOR:
        return NamedStart(None, OTHER_EXECUTOR, text.OTHER_EXECUTOR)
    return None


def not_started(db, node_id: str, plan, holder) -> tuple[str, str]:
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
