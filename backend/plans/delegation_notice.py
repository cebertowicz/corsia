"""What a parent learns about the plan work it delegated: a digest, or a wake.

The one owner of "the parent was told". A delegated worker's ordinary progress
is a **digest** its parent reads at its next Turn boundary; the parent is
**woken** only when it has something to do. Keeping both channels here means
there is one table of what the parent hears, one place that decides which
channel carries it, and no second module that can answer the same question
differently.

Two things this module deliberately does not decide:

- **Whether the parent must act on a failure.** A failed step is only a
  digest here; whether it leaves the parent something to decide is known
  only when the worker's Turn ends (``plans.delegation_turn_end``), which
  wakes through ``_wake_parent`` below, the one writer of parent wakes.
- **The worker's return.** ``plans.delegation_harvest`` carries the result to
  the parent to judge. A digest here never doubles as that verdict.
"""

from __future__ import annotations

import json

#: Every notice is about the worker, so each label opens with the same subject
#: and the step's title completes it. The parent reads a flat list of these.
_STEP = "Delegated step"

#: A delegated step's own settled outcome, as the plan that delegated it hears
#: of it. The result reaches the parent to act on at harvest, so these stay
#: passive facts: the parent is not woken to judge one step's terminal.
_TERMINAL_DIGESTS = {
    "accepted": ("plan.child_node_completed", f"{_STEP} accepted"),
    "accepted_with_followup": (
        "plan.child_node_completed", f"{_STEP} accepted with follow-up",
    ),
    "failed": ("plan.child_node_failed", f"{_STEP} failed"),
    "closed": ("plan.child_node_closed", f"{_STEP} closed"),
    "review_requested": (
        "plan.child_node_review_requested", f"{_STEP} put up for review",
    ),
}

#: Conditions inside the worker's own plan. None of them is the parent's to
#: clear: the worker sequenced its own work, or is waiting on something.
_EVENT_DIGESTS = {
    "dependency_added": (
        "plan.child_dependency_added", "Delegated work sequenced",
    ),
    "watch_armed": ("plan.child_watch_armed", f"{_STEP} waiting"),
    "watch_resolved": ("plan.child_watch_resolved", f"{_STEP} wait ended"),
    "watch_expired": ("plan.child_watch_expired", f"{_STEP} wait expired"),
    "decision_answered": (
        "plan.child_decision_answered", "Decision answered for delegated work",
    ),
}

#: A user's own pause is labeled as theirs wherever it appears, because no
#: agent may act on it (matrix rule 6): not the worker, and not the parent.
_BY_USER = " by user"


def _payload(payload) -> dict:
    """Return one event payload as a dict, whether stored or still in hand."""
    if isinstance(payload, dict):
        return payload
    try:
        decoded = json.loads(payload or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _by_user(actor_id: str | None) -> bool:
    """Whether this event was the user's own act, not any agent's."""
    from plans.boundary import is_user_audit_id

    return is_user_audit_id(actor_id)


def _delegation(connection, plan_id: str) -> tuple[str, str] | None:
    """Return (parent plan, worker thread) while the parent still holds it."""
    from plans.projections import live_delegation

    row = connection.execute(
        "SELECT owner_kind,owner_id,parent_plan_id FROM plans WHERE id=?",
        (plan_id,),
    ).fetchone()
    if row is None or row["owner_kind"] != "thread" or row["parent_plan_id"] is None:
        return None
    worker = str(row["owner_id"])
    parent_plan_id = str(row["parent_plan_id"])
    if not live_delegation(connection, worker, parent_plan_id):
        return None
    return parent_plan_id, worker


def _hold_reason(connection, hold_id: str | None) -> str:
    """Return why a hold was placed, from the hold itself."""
    if not hold_id:
        return ""
    row = connection.execute(
        "SELECT reason FROM plan_node_holds WHERE id=?", (hold_id,),
    ).fetchone()
    return "" if row is None else str(row["reason"] or "")


def _hold_digest(
    connection, event: str, payload: dict, *, by_user: bool,
) -> tuple[str, str, dict] | None:
    """Return the notice for a pause placed, a blocker raised or one cleared.

    A blocker for someone other than the user to clear is a wait, whose
    arming wakes the parent instead (matrix row B4). A blocker hold is always
    the user's to clear: the parent learns of it, but it is not the parent's.
    """
    kind = payload.get("kind")
    if event == "hold_placed" and kind == "blocked":
        hold_id = payload.get("hold_id")
        # The summary already says when the user placed it on themselves.
        return (
            "plan.child_blocker_raised",
            f"{_STEP} blocked{'' if by_user else ' on the user'}",
            {"reason": _hold_reason(connection, hold_id), "hold_id": hold_id},
        )
    if event == "hold_placed" and kind == "paused":
        reason = _hold_reason(connection, payload.get("hold_id"))
        return (
            "plan.child_node_paused", f"{_STEP} paused",
            {"reason": reason, "hold_id": payload.get("hold_id")},
        )
    if event == "hold_cleared" and kind == "blocked":
        return (
            "plan.child_blocker_cleared", f"{_STEP} blocker cleared",
            {"hold_id": payload.get("hold_id")},
        )
    return None


def _watch_digest(payload: dict) -> dict:
    """Return the awaited event and deadline a watch notice carries."""
    condition = _payload(payload.get("condition"))
    return {
        "expected_event": payload.get("expected_event"),
        "condition_type": condition.get("type"),
        "condition_value": condition.get("value"),
        "watch_id": payload.get("watch_id"),
    }


def _node_digest(
    connection, event: str, new_state: str | None, payload: dict, *, by_user: bool,
) -> tuple[str, str, dict] | None:
    """Return (change kind, label, extra details) for one worker node event."""
    if event == "state_transition":
        found = _TERMINAL_DIGESTS.get(str(new_state or ""))
        if found is None:
            return None
        kind, label = found
        reason = str(payload.get("reason") or "")
        return kind, label, ({"reason": reason} if reason else {})
    if event in {"hold_placed", "hold_cleared"}:
        return _hold_digest(connection, event, payload, by_user=by_user)
    found = _EVENT_DIGESTS.get(event)
    if found is None:
        return None
    kind, label = found
    extra = _watch_digest(payload) if event.startswith("watch_") else {}
    return kind, label, extra


def _summary(label: str, title: str, details: dict, *, by_user: bool) -> str:
    """Return one digest line: what happened to which step, and the why."""
    line = f"{label}{_BY_USER if by_user else ''}: {title}"
    reason = str(details.get("reason") or "")
    if reason:
        return f"{line} — {reason}"
    awaited = details.get("expected_event")
    if awaited:
        # Only a deadline has a "until <value>" to add; an event condition's
        # own value is the awaited event's own name, so appending it there
        # reads as "awaiting reviewer.verdict until reviewer.verdict".
        deadline = details.get("condition_value")
        waiting = (
            f" until {deadline}"
            if details.get("condition_type") == "deadline" and deadline
            else ""
        )
        return f"{line} — awaiting {awaited}{waiting}"
    return line


def observe_node_event(
    connection,
    *,
    node_id: str,
    event: str,
    event_id: str,
    actor_id: str | None,
    new_state: str | None,
    payload,
    created_at: str,
) -> None:
    """Tell the delegating plan, passively, what its worker just did.

    Reached from ``plans.service._event``, so every durable node event in a
    worker's plan passes here exactly once and the notice commits with the
    write that caused it.

    The parent's own verdict on a step is the parent's receipt, as any plan
    event it authored is; everything else is the worker's doing, so an
    unattributed transition is credited to the worker rather than mistaken for
    the parent's.
    """
    row = connection.execute(
        "SELECT plan_id,title FROM plan_nodes WHERE id=?", (node_id,),
    ).fetchone()
    if row is None:
        return
    delegation = _delegation(connection, str(row["plan_id"]))
    if delegation is None:
        return
    parent_plan_id, worker = delegation
    if event == "revision_created":
        from plans import delegation_friction

        delegation_friction.observe(
            connection,
            node_id=node_id,
            plan_id=str(row["plan_id"]),
            title=str(row["title"]),
            event_id=event_id,
            actor_id=actor_id,
            parent_plan_id=parent_plan_id,
            worker=worker,
            created_at=created_at,
        )
        return
    payload_dict = _payload(payload)
    from plans import delegation_decisions

    if delegation_decisions.notify(
        connection, node_id=node_id, row=row, event=event, event_id=event_id,
        actor_id=actor_id, payload=payload_dict, created_at=created_at,
        parent_plan_id=parent_plan_id, worker=worker,
    ):
        return
    found = _node_digest(
        connection, event, new_state, payload_dict, by_user=_by_user(actor_id),
    )
    if found is None:
        return
    change_kind, label, details = found
    _append(
        connection,
        plan_id=parent_plan_id,
        event_id=event_id,
        actor_id=actor_id or worker,
        change_kind=change_kind,
        summary=_summary(
            label, str(row["title"]), details, by_user=_by_user(actor_id),
        ),
        details={
            **details,
            "node_id": node_id,
            "child_plan_id": str(row["plan_id"]),
            "worker_thread_id": worker,
            "state": new_state,
        },
        created_at=created_at,
    )


def observe_plan_transition(
    connection,
    *,
    plan_id: str,
    event_id: str,
    actor_id: str | None,
    new_state: str | None,
    created_at: str,
) -> None:
    """Report a worker stopping or restarting its whole plan to its parent.

    A worker that paused its own plan stopped doing the work the parent is
    waiting for, so the parent is woken to tell the user if the step is needed
    now (only the user resumes, pause contract rule 1). Only the worker's own
    act says that: a pause the *user* set is never any agent's to undo (matrix
    rule 6), and one the parent or nobody set is not the worker's doing, so
    each reaches the parent as a digest and nudges nobody.
    """
    if str(new_state or "") != "paused":
        return
    delegation = _delegation(connection, plan_id)
    if delegation is None:
        return
    parent_plan_id, worker = delegation
    if actor_id != worker:
        _append(
            connection,
            plan_id=parent_plan_id,
            event_id=event_id,
            actor_id=worker,
            change_kind="plan.child_plan_paused",
            summary=f"Delegated plan paused{_BY_USER if _by_user(actor_id) else ''}",
            details={"child_plan_id": plan_id, "worker_thread_id": worker},
            created_at=created_at,
        )
        return
    _wake_parent(
        connection, parent_plan_id, worker,
        kind="child_paused_itself", subject_id=plan_id, child_plan_id=plan_id,
    )


def _append(
    connection,
    *,
    plan_id: str,
    event_id: str,
    actor_id: str | None,
    change_kind: str,
    summary: str,
    details: dict,
    created_at: str,
) -> None:
    """Write one notice at the receiving plan owner's own boundary."""
    from plans.plan_events import append_owner_event

    append_owner_event(
        connection,
        plan_id=plan_id,
        event_id=event_id,
        actor_id=actor_id,
        event_type=change_kind,
        summary=summary,
        details=details,
        created_at=created_at,
    )


def _wake_parent(
    connection, parent_plan_id: str, worker: str, *, kind: str, subject_id: str,
    child_plan_id: str | None = None, revision: int | None = None,
    actor_id: str | None = None, extra: dict | None = None, account_hold_state=None, **fields,
) -> str | None:
    """Wake the parent about its worker, through the one plan-input writer.

    Returns the wake's input id, or None when no wake was written (the parent
    cannot take input, or there is nothing left for it to decide).

    By default the wake is identified by the *worker's* plan revision, so a
    later occurrence is new; a caller whose own occurrence differs passes its
    own. What the parent itself did (``actor_id``) is its receipt, never a
    wake. ``fields`` fill the wake line; ``extra`` adds facts to the JSON
    tail beyond node/worker, for a wake one line cannot carry (E1's draft).
    A delegating step already settled has nothing left for the parent to
    decide, so its worker's late news is not a wake (#1991). The step is
    ``projections.delegated_step``'s: a settled earlier step that named the
    same worker is not the one the news is about (#3285).
    """
    from plans import plan_wake, projections, service
    from prompts import plan_wakes as text

    plan = service._plan_row(connection, parent_plan_id)
    if plan["owner_kind"] != "thread" or actor_id == str(plan["owner_id"]):
        return None
    node = projections.delegated_step(connection, parent_plan_id, worker)
    if node is None:
        return None
    template = (text.ACCOUNT_PENDING_RESUME_WAKE_TEMPLATE if account_hold_state == 'pending_resume'
                else text.ACCOUNT_HELD_WAKE_TEMPLATE if account_hold_state == 'unready'
                else getattr(text, f"WAKE_{kind.upper()}"))
    line = template.format(
        **{"title": str(node["title"]), "worker": worker, "step": str(node["id"]), **fields},
    )
    if revision is None:
        revision = int(service._plan_row(connection, child_plan_id)["revision"])
    details = {"node_id": node["id"], "worker_thread_id": worker, **(extra or {})}
    return plan_wake.append(
        connection,
        recipient=str(plan["owner_id"]),
        kind=kind,
        subject_id=subject_id,
        revision=revision,
        plan_id=parent_plan_id,
        content=f"{line}\n{json.dumps(details, sort_keys=True)}",
        delegated_step_id=str(node["id"]),
        delegated_worker=worker,
        account_hold_state=account_hold_state,
    )
