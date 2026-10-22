"""The one writer for a plan result another thread needs to learn about.

A thread learns about plan work it did not do itself in exactly one way: this
writer appends the result to that thread's own input FIFO, inside the write that
caused it. One writer, so there is one place that decides what such an input
looks like, one identity rule, and one answer to "was this thread told?".

What it will not do:

- **Wake the actor that caused the result.** A thread that closed its own task,
  or whose own call settled and took its next task, has already seen it. The
  causing executor is named by the caller, because the write knows it and the
  audit actor does not (a dispatch settle carries no actor).
- **Wake a thread that cannot take input.** An archived, purged or still
  model-less recipient is skipped, because the plan state that gave it work is
  durable and liveness wakes it once it can take input. Every other rejection
  is a defect, and is raised rather than hidden.

The identity of the input is ``plan-wake:{kind}:{subject}:{revision}``, with
``:{about}`` when the result concerns a second node the subject alone does not
name: the same result appended twice is the same input, and a later result
about the same subject is a new one. The words come from ``prompts.plan_wakes``; the facts come
from Plans.
"""

from __future__ import annotations

import json
from typing import Literal

#: A task this thread executes was closed by another actor.
TASK_CLOSED = "task_closed"
#: Another actor's write gave this thread's slot to a task.
WORK_SELECTED = "work_selected"
#: An artifact ask on a task this thread runs was answered or retired; the task no longer waits.
ASK_RELEASED = "ask_released"
#: Work this thread delegated has returned and awaits its verdict.
CHILD_RETURNED = "child_returned"
#: A submission this thread made or returned was rejected; it now holds the revision.
REVISION_REQUESTED = "revision_requested"
#: A task one of this thread's tasks depends on settled: in another plan, or
#: failed or closed by someone else in its own (``plans.cross_plan``).
PREREQUISITE_SETTLED = "prerequisite_settled"
#: A conductor-added task in another agent's plan reached a terminal outcome.
CONDUCTOR_TASK_SETTLED = "conductor_task_settled"

def wake_id(kind: str, subject_id: str, revision: int, about: str | None = None) -> str:
    """The durable identity of one wake, so a reader can ask whether it was sent."""
    return f"plan-wake:{kind}:{subject_id}:{revision}" + (f":{about}" if about else "")


def append_input(
    db,
    *,
    recipient: str,
    message_id: str,
    content: str,
    metadata: dict,
    continuation: dict | None = None,
    skip_when_model_less: bool = False,
):
    """Append one result-bearing plan input to a thread's own FIFO.

    The single place this domain turns a plan result into an input: one
    admission guard, one digest boundary, one role/intent/delivery shape (a
    system context-event wake, like every routed event). What
    genuinely differs between results is passed in -- identity, words,
    metadata, whether a currency condition travels with it, and whether a
    model-less recipient is a skip or takes the input to hold for a model --
    so the kinds cannot drift apart into two writers again.

    Returns the accepted input, or None when the recipient cannot take input.
    """
    from infra.input_admission import accept_thread_input
    from infra.passive_changes import materialize_pending_digest
    from infra.thread_inputs import thread_admission_state

    state = thread_admission_state(db, recipient, allow_unavailable=True)
    if state is None:
        return None
    if skip_when_model_less and not str(state["model"] or "").strip():
        # A wake is not invented for a thread that cannot run it: the plan
        # keeps the work pending and offers it again once a model exists.
        return None
    materialize_pending_digest(db, recipient)
    from prompts import plan_wakes as wakes

    # A plan input is a context event, in the one shape every event wake has
    # (``events.delivery``): a system row the transcript renders as an event
    # chip and the model reads as provider input; never a user's own words.
    # The chip's line is its own, not the agent's first line of ``content``.
    shaped = {
        "type": "context_event",
        "event_disposition": "wake",
        "summary": wakes.account_wake_chip(metadata['event_type'], metadata.get('account_hold_state')),
        **metadata,
    }
    return accept_thread_input(
        recipient,
        role="system",
        content=content,
        intent="apply_now",
        message_id=message_id,
        metadata=shaped,
        delivery_options={"native_mode": "event", "attachments": []},
        continuation=continuation,
        db=db,
    )


def append(
    db,
    *,
    recipient: str,
    kind: str,
    subject_id: str,
    revision: int,
    plan_id: str,
    content: str,
    delegated_step_id: str | None = None,
    delegated_worker: str | None = None,
    about: str | None = None,
    open_node_id: str | None = None,
    account_hold_state: Literal['unready', 'pending_resume'] | None = None,
) -> str | None:
    """Append one news wake to a recipient thread.

    Returns the input's durable id, or None when the recipient cannot take
    input or has nothing left to act on. Runs inside the caller's write, so the
    result and the input that reports it commit together. A wake about a
    delegated step's worker names that step and worker (``delegated_step_id``,
    ``delegated_worker``): it stays worth a Turn while the step is open, free
    of holds, and still delegated to that worker (``wake_currency``). A wake
    naming ``open_node_id`` stays worth one while that task is open.
    The duplicate check before the insert guarantees one input per identity,
    whatever text a repeat carries: an existing identity returns that input.
    Repeated observations cannot re-send even when their text changes.
    """
    from automation.continuation.models import StaleContinuation
    from plans import wake_currency
    from prompts.plan_wakes import validate_account_hold_state

    validate_account_hold_state(f'plan.{kind}', account_hold_state)
    message_id = wake_id(kind, subject_id, revision, about)
    if db.execute("SELECT 1 FROM messages WHERE id=?", (message_id,)).fetchone():
        return message_id
    try:
        accepted = append_input(
            db,
            recipient=recipient,
            message_id=message_id,
            content=content,
            metadata={
                "event_type": f"plan.{kind}",
                "origin": "plan",
                "plan_id": plan_id,
                "node_id": subject_id,
                **({'account_hold_state': account_hold_state} if account_hold_state is not None else {}),
            },
            continuation=wake_currency.condition(
                plan_id=plan_id, kind=kind, subject_id=subject_id, revision=revision,
                delegated_step_id=delegated_step_id,
                delegated_worker=delegated_worker,
                open_node_id=open_node_id,
            ),
            skip_when_model_less=True,
        )
    except StaleContinuation:
        # The wake stopped being worth a Turn between deciding to send it and
        # admitting it. Skipping that is this writer's whole point. Every other
        # rejection is a defect and fails the write rather than losing the
        # result silently.
        return None
    return None if accepted is None else message_id


def recipients(reconciled, *, causing_executor: str | None) -> tuple[str, ...]:
    """Return the executors a reconciliation newly gave work to, minus the actor.

    The executor whose own write this was already knows: it is running, and its
    result carries the same facts. Everyone else learns here.
    """
    return tuple(
        dict.fromkeys(
            executor
            for _node_id, executor in reconciled.selected
            if executor and executor != causing_executor
        )
    )


def selected_fact(db, reconciled, executor: str) -> dict:
    """Return the handoff fact for the task a reconciliation gave ``executor``.

    A write that wakes ``executor`` itself, as ``causing_executor``, stands in
    for its work-selected wake, so it carries this beside its own ``next`` and
    the task arrives with the same description either way (``handoff_brief``).
    Empty when the reconciliation selected nothing for ``executor``.
    """
    from plans import handoff_brief

    for node_id, selected in reconciled.selected:
        if selected == executor:
            return handoff_brief.fact(db, node_id, executor)
    return {}


def with_next(db, plan_id: str, executor: str, line: str, **facts) -> str:
    """Return one wake's text: what happened, then the same facts a close returns."""
    from memory import node_start_recall
    from plans import close_next

    block = close_next.executor_next(db, plan_id, executor)
    prior_words = node_start_recall.related_for_next(db, block, caller_thread_id=executor)
    if prior_words:
        facts[node_start_recall.RESULT_KEY] = prior_words
    return f"{line}\n{json.dumps({**facts, 'next': block}, sort_keys=True)}"


#: The result wake is bounded like every default read (brief: numeric budgets).
RETURNED_STEPS_MAX = 12
RETURNED_TITLE_MAX_BYTES = 96
RETURNED_EVIDENCE_MAX_BYTES = 480

def notify_returned(
    db, *, node_id: str, child_plan_id: str, worker_thread_id: str, reply: str = "",
) -> str | None:
    """Tell the parent its delegated work has returned, with the result to judge."""
    from plans import plan_wake_returned

    return plan_wake_returned.notify(
        db,
        node_id=node_id,
        child_plan_id=child_plan_id,
        worker_thread_id=worker_thread_id,
        reply=reply,
    )


def notify_revision(
    db, node_id: str, *, recipient: str, feedback: str, worker_step_id: str | None = None,
) -> str | None:
    """Tell the revision holder it was rejected and name its worker step.

    Criteria and feedback travel whole up to their ceiling because a worker
    cannot read its parent's node alone (#2835). The wake remains current
    while the revision is the worker's open, unheld step (#1731).
    """
    from plans import handoff_brief, service
    from plans.close_next_bounds import REVISION_BRIEF_MAX_BYTES as cap, bounded
    from prompts import plan_wakes as wakes

    with service._connection(db) as connection:
        node = _node(connection, node_id)
        own_plan = service.ensure_plan("thread", recipient, db=connection)
        to_worker = node["spawned_thread_id"] == recipient
        field = handoff_brief.bounded_field(connection, node, own_plan.id)
        return append(
            connection,
            recipient=recipient,
            kind=REVISION_REQUESTED,
            subject_id=node_id,
            revision=int(node["revision"]),
            plan_id=str(node["plan_id"]),
            delegated_step_id=node_id if to_worker else None,
            delegated_worker=recipient if to_worker else None,
            content=with_next(
                connection, own_plan.id, recipient,
                (wakes.REVISION_WORKER_STEP if worker_step_id
                 else wakes.WAKE_REVISION_REQUESTED).format(
                    title=str(node["title"]), worker_step_id=worker_step_id, revision_id=node_id,
                ),
                revision={
                    "node_id": node_id,
                    "worker_step_id": worker_step_id,
                    "revision": int(node["revision"]),
                    "title": bounded(str(node["title"]), RETURNED_TITLE_MAX_BYTES),
                    "acceptance_criteria": field(
                        str(node["acceptance_criteria"]), cap, what="criteria",
                    ),
                    "feedback": field(feedback, cap, what="feedback"),
                },
            ),
        )


def notify_selected(
    db, reconciled, *, plan_id: str, causing_executor: str | None,
    from_worker: bool = False,
) -> None:
    """Tell each executor whose slot this write filled that it has current work.

    A delegated step its worker's side hands back is the notification
    matrix's news (``delegated_selection.told_elsewhere``), not this wake's.
    A task its executor did not write brings its description, bounded, beside
    the block (``handoff_brief``).
    """
    from plans import delegated_selection, handoff_brief
    from prompts import plan_wakes as wakes

    for node_id, executor in reconciled.selected:
        if not executor or executor == causing_executor:
            continue
        node = _node(db, node_id)
        if delegated_selection.told_elsewhere(db, executor, node, from_worker=from_worker):
            continue
        append(
            db,
            recipient=executor,
            kind=WORK_SELECTED,
            subject_id=node_id,
            revision=int(node["revision"]),
            plan_id=plan_id,
            content=with_next(
                db, plan_id, executor,
                wakes.WAKE_WORK_SELECTED.format(title=str(node["title"])),
                **handoff_brief.fact(db, node_id, executor),
            ),
        )


def notify_closed(
    db, node_id: str, *, plan_id: str, executor: str | None, causing_executor: str | None,
) -> None:
    """Tell a task's executor that somebody else took it out of their hands.

    Whether a close happened is read from the task's state, never from which
    boundary wrapper called. A rejection hands the work back to the same
    executor, and reporting that as a close would lie about the task and give
    one revision two contradictory instructions. ``OPEN_TASK_STATES`` is
    already the definition of work still in its executor's hands, so this
    reuses it rather than deciding the question a second way.
    """
    from plans import node_order
    from prompts import plan_wakes as wakes

    if not executor or executor == causing_executor:
        return
    node = _node(db, node_id)
    if node["state"] in node_order.OPEN_TASK_STATES:
        return
    append(
        db,
        recipient=executor,
        kind=TASK_CLOSED,
        subject_id=node_id,
        revision=int(node["revision"]),
        plan_id=plan_id,
        content=with_next(
            db, plan_id, executor,
            wakes.WAKE_TASK_CLOSED.format(title=str(node["title"])),
        ),
    )


def _node(db, node_id: str):
    from plans import service

    return service._node_row(db, node_id)
