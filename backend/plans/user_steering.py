"""The user steers any open row: pause or resume it; Mark done and Won't do settle it.

The plan pane's right-click menu posts the ordinary ``pause_node``,
``resume_node`` and ``close_node`` operations, plus the user's ``mark_done``,
so an agent's tool and the user share one vocabulary. Mark done and Won't do
go to ``plans.user_settle``; pause and resume take ``plans.boundary_dispatch``'s
normal path, and this module adds only what the user's word adds:

* a user pause is a placed pause hold whose ``owner_kind`` is ``user``. Only
  the user clears it. The user also resumes any pause an agent placed before
  pause became the user's, which nothing else would ever end;
* a task handed to a live worker is paused and resumed as the worker's whole
  plan, the user's ordinary plan pause. No task hold is placed, and the
  delegating agent hears of it through ``plans.delegation_notice``;
* the owning agent is told in its passive digest, one line through
  ``plans.plan_events.append_owner_event``, never a wake.

``offered`` is the one owner of which of these the pane shows on a row, and it
reads the same facts ``prepare`` checks on the server.
"""

from __future__ import annotations

from typing import Any

import plans.pause_authority as pause_authority
import plans.service as service
from plans.errors import PlanRefusal

#: The hold reason of a pause the user placed from the pane.
USER_PAUSE_REASON = "Paused by the user"
#: The event type the owner's passive digest carries these lines under.
CHANGE_KIND = "plan.user_task_action"

#: Operations the user applies to any open row, in their agent-tool names.
STEERING_OPERATIONS = frozenset({"pause_node", "resume_node", "close_node", "mark_done"})
_OPEN_STATES = frozenset({"pending", "active", "review_requested"})


def is_user_pause(hold: dict) -> bool:
    """Whether this hold is a pause the user placed.

    Older user pauses were recorded as ``owner_kind='user'`` by schema 59.
    """
    return hold["kind"] == "paused" and hold["owner_kind"] == "user"


def placed_pause(causes) -> dict | None:
    """Return the pause hold placed on this task, ignoring derived causes.

    'Plan is paused' and 'Its worker is paused' are derived from other plans'
    state; neither is a hold on this task, so neither decides its controls.
    """
    return next(
        (cause for cause in causes
         if cause.get("placed") and cause.get("kind") == "paused"),
        None,
    )


def _pause_control(
    causes, worker_plan_state: str | None, plan_state: str,
) -> str | None:
    """Return 'pause_node', 'resume_node' or None, from one set of facts."""
    if placed_pause(causes) is not None:
        return "resume_node"
    if worker_plan_state in {None, "active"}:
        return "pause_node"
    if worker_plan_state == "paused" and plan_state == "active":
        return "resume_node"
    return None


def offered(
    node: dict, review: dict | None, causes, *,
    plan_state: str, worker_plan_state: str | None,
) -> list[str]:
    """Return the controls the pane shows on this row, in display order.

    Every open row offers Mark done and Won't do, whoever it is assigned to and
    whatever holds it (``plans.user_settle``). A task adds Pause, or Resume
    when a pause holds it; a task handed to a live worker offers Pause while the
    worker's plan is active and Resume while it is paused and this plan is not.
    A question put to the user offers only Won't do: answering it, in the
    attention queue, is what finishes it.
    """
    if node["state"] not in _OPEN_STATES:
        return []
    if review is not None and review["kind"] == "human_decision":
        return ["close_node"]
    actions = ["mark_done"]
    control = (
        _pause_control(causes, worker_plan_state, plan_state)
        if node["kind"] == "task" else None
    )
    if control is not None:
        actions.append(control)
    return [*actions, "close_node"]


def refuse_agent_resume(db, node_id: str, hold_id: str) -> None:
    """Refuse a thread clearing the user's pause on a task."""
    from plans import holds

    hold = next(
        (held for held in holds.active_holds(node_id, db=db) if held["id"] == hold_id),
        None,
    )
    if hold is not None and is_user_pause(hold):
        raise PlanRefusal(pause_authority.NODE_RESUME_REFUSAL)


def worker_plan(db, node) -> Any | None:
    """Return the live worker's plan this task hands work to, or None.

    The same attachment the plan tree shows as the task's child plan
    (``hierarchy.shown_worker_plan``), so ``offered`` and ``prepare`` agree.
    """
    from plans import hierarchy

    return hierarchy.shown_worker_plan(db, node)


def prepare(operation: str, params: dict[str, Any], db) -> dict[str, Any]:
    """Fill what the pane does not send to a pause or resume; refuse what it may not do.

    Pure: it writes nothing. A pause or resume that acts on the worker's plan
    comes back with ``worker_plan_id`` set, and no task hold is touched.
    """
    from plans import admission

    node = service._node_row(db, params["node_id"])
    causes = admission.project(node["id"], db=db)["causes"]
    pause = placed_pause(causes)
    worker = None if pause else worker_plan(db, node)
    control = _pause_control(
        causes, worker["state"] if worker is not None else None,
        service._plan_row(db, node["plan_id"])["state"],
    )
    if control != operation:
        raise service.PlanError(
            "this task is already paused" if operation == "pause_node"
            else "this task is not paused"
        )
    if worker is not None:
        return {**params, "worker_plan_id": str(worker["id"])}
    if pause is None:
        return {"reason": USER_PAUSE_REASON, **params}
    return {**params, "hold_id": pause["id"]}


def move_worker_plan(operation: str, params: dict[str, Any], common: dict) -> dict:
    """Pause or resume the worker's whole plan as the user, then report the task.

    The ordinary user plan pause, with its own cascade, wake and parent notice
    (``delegation_notice.observe_plan_transition``), so nothing else is sent.
    """
    from plans import admission, lifecycle

    db = common["db"]
    plan = service._plan_row(db, params["worker_plan_id"])
    moved = lifecycle.transition(
        str(plan["id"]), "paused" if operation == "pause_node" else "active",
        expected_revision=plan["revision"], **common,
    )
    return {
        **admission.result(params["node_id"], db=db),
        "cascaded_plan_ids": [str(plan["id"]), *moved["cascaded_plan_ids"]],
    }


_TOLD = {
    "pause_node": ("paused", "do not work it until they resume it", "hold_placed", None),
    "resume_node": ("resumed", "it is yours again", "hold_cleared", None),
    "close_node": ("closed", "do not work it", "state_transition", "closed"),
    "mark_done": ("marked done", "do not work it", "state_transition", "accepted"),
}


def tell_owner(
    operation: str, params: dict[str, Any], common: dict, *, below: int = 0,
) -> None:
    """Leave the owning agent one digest line saying what the user did to its row.

    Called after the change applied, so the line is attributed to the event it
    recorded. ``below`` counts the open rows that settled with it.
    """
    from plans.plan_events import append_owner_event

    db = common["db"]
    node = service._node_row(db, params["node_id"])
    verb, detail, event, new_state = _TOLD[operation]
    cause = db.execute(
        """SELECT id, created_at FROM plan_node_events
           WHERE node_id=? AND event=? AND (? IS NULL OR new_state=?)
           ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        (node["id"], event, new_state, new_state),
    ).fetchone()
    why = f" ({params['reason']})" if operation == "close_node" else ""
    steps = ""
    if below:
        steps = f" and its {below} open step{'s' if below > 1 else ''}"
        detail = detail.replace("work it", "work them")
    append_owner_event(
        db, plan_id=str(node["plan_id"]), event_id=str(cause["id"]),
        actor_id=common["actor_id"], event_type=CHANGE_KIND,
        summary=f'The user {verb} {node["kind"]} "{node["title"]}"{steps}{why}: {detail}.',
        details={"node_id": str(node["id"]), "action": verb},
        created_at=cause["created_at"],
    )

