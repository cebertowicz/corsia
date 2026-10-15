"""Model-facing text for structured plan tool results.

Text constants and text formatting functions only (package rule). The plans
domain decides what a closing result says; this module owns every word an
agent reads in its ``next`` block, including its truncation markers, the one
condition formatter, and the one byte-bounded truncation helper bounded plan
text shares. The helper takes the caller's measure and marker, so each caller
bounds text by the size it actually emits.
"""

from __future__ import annotations

import math
from collections.abc import Callable

#: Appended to the plan mutation tool's description.
CLOSE_NEXT_DESCRIPTION = (
    "Closing operations (request_review, fail, accept, accept_with_follow_up, "
    "close_node, archive_node) and parking operations (block_node, arm_watch) "
    "return next: the task now holding your slot and why (if newly started: "
    "its criteria, plus selected_description beside next when someone else "
    "wrote it), up to 3 of your other open tasks with id and "
    "revision, and their total. Pass next_node_id to start one in the same "
    "call; next.named says if it did not start and why."
)

#: Appended to the plan mutation tool's description after ``CLOSE_NEXT_DESCRIPTION``.
USER_TASKS_DESCRIPTION = (
    "assignee='user' only for their hands, presence or missing credential, after trying "
    "yourself (host lease too); description: how-to, then 'Already tried:'. Decisions are "
    "for choices. You may close or settle user tasks."
)

#: The next steps after a thread cannot mutate another agent's plan.
FOREIGN_PLAN_MUTATION = (
    "Only a designated conductor may append a top-level task to another agent's "
    "plan. Instead, depend on the lane's node (plan_update add_dependency after "
    "plan_read(thread_id=...)), ask the lane for a review, or file it to the "
    "backlog under the lane's label."
)
#: The target plan must exist before a conductor can append work to it.
FOREIGN_PLAN_MISSING = (
    "No plan has that id. To add to another agent's plan, use the id "
    "plan_read(thread_id=...) returns; an agent with no plan must create one first."
)

#: The schema description of a closing operation's optional named next task.
NEXT_NODE_ID_DESCRIPTION = (
    "Optional: your pending or active task in this plan to start once this "
    "operation frees your slot; next.named says if it could not."
)

#: Ends a label (title, wait, reason) cut to its byte bound.
ELLIPSIS = "…"
#: Ends acceptance criteria cut to fit the block. No author writes it, so a cut
#: cannot read as the criteria's own text, and it names the call that returns
#: them whole: a start of the task already holding the slot changes nothing.
CRITERIA_CUT = "… [cut to fit; starting this task returns its full criteria]"
#: Ends rejection feedback cut to fit the block, naming the read that returns
#: it whole: the task's detail read carries its review events unbounded.
FEEDBACK_CUT = "… [cut to fit; plan_read with this node_id returns its full feedback]"
#: Ends a revision handoff's feedback or criteria, or a handed-over task's
#: description, past its ceiling, naming the read its holder can make: a
#: worker's revision lives in its parent's plan.
REVISION_CUT = "… [cut to fit; plan_read({where}node_id='{node_id}') returns its full {what}]"


def revision_field(
    value: str, max_bytes: int, *, measure: Callable[[str], int],
    node_id: str, what: str, plan_thread: str | None,
) -> str:
    """One revision handoff field, cut only past ``max_bytes``.

    ``plan_thread`` names the plan holding the revision when it is not the
    holder's own, so the cut names a read the holder can make.
    """
    where = "" if plan_thread is None else f"thread_id='{plan_thread}', "
    marker = REVISION_CUT.format(where=where, node_id=node_id, what=what)
    return bounded_text(value, max_bytes, measure=measure, marker=marker)

#: Why a named next task did not start.
UNKNOWN_TASK = "no such task in this plan"

#: The one extra status fact every delegated worker needs before it can substitute
#: for an input it cannot access (N7); ``plans.status_context`` carries it.
DELEGATED_PLAN_GUIDANCE = (
    "If a criterion needs something you can't open or reach, block_node with "
    "owner_kind 'parent' and say what; don't substitute your own version."
)


def send_back_round_guidance(round_number: int, model: str) -> str:
    """Return the parent-facing routes after a worker's repeated send-back."""
    return (
        f"Round {round_number}; worker model: {model}. Re-brief the worker, move it to a "
        "stronger model with delegate_set_model, replace it with a fresh worker using "
        "replace_spawned_thread, take the step over (do it yourself, then mark_done with "
        "evidence once the worker is idle), or raise to the user if its criteria cannot be met."
    )

ARCHIVED_TASK = "task is archived"
TASK_STATE = "task is {state}"
NODE_KIND = "node is a {kind}"
DECISION_OWNED = "owned by an open user decision"
USER_ASSIGNED = "assigned to the user"
OTHER_EXECUTOR = "run by a different executor"

HAS_CHILDREN = "task has child nodes"
PLAN_STATE = "plan is {state}"

_SLOT_HELD = "executor slot held by {title}; {action} this task after {after}"
_AFTER_CLOSE = "that task closes"
_LISTED = "{shown} of {total} other open tasks"
_IDLE_LISTED = "{shown} of {total} open tasks, none ready, soonest-ending first"
_IDLE_LISTED_SET_ASIDE = "{shown} of {total} open tasks, none ready, set-aside work first in plan order"
#: Said when nothing is active or ready and no wait ends soon (W9).
IDLE_PICK_UP = "Nothing is active. If a waiting task can move now, start it."
_IDLE_PICK_UP_SET_ASIDE = (
    "Nothing is active. You scheduled {set_aside} of these yourself; nothing will release them "
    "before their date unless you do. They are listed in your plan's order: start the top one you "
    "can drive now (its release_wait), or reorder them if that order is wrong."
)


#: Opens the plan-upkeep line beside a Turn's first tool result, so it cannot be
#: read as the tool's own output.
UPKEEP_LABEL = "[CORSIA plan notice]"
_UPKEEP_ACTIVE = (
    "Current task: {title} — active. Update it if it is finished, blocked, "
    "or you have moved to different work."
)
_UPKEEP_HELD = (
    "Current task: {title} — {wait}. Leave it held while that still applies; "
    "update it if the work changed."
)
_UPKEEP_START = (
    "No plan task is selected. Startable: {tasks}. Start one with "
    "plan_update start node_id={first_id} expected_revision={first_revision} "
    "when you begin it, or update the plan if the work changed."
)
_UPKEEP_WAITING = (
    "No plan task is selected. Waiting: {tasks}. If this work belongs to one, "
    "update that task; otherwise add it with plan_update add, then start it."
)
UPKEEP_ADD_ROUTE = (
    f"{UPKEEP_LABEL} No plan task is selected and none can be started. Add the "
    "work with plan_update add, then start it."
)


def _named_tasks(tasks: list[tuple[str, str]]) -> str:
    return "; ".join(f"{title} ({node_id})" for title, node_id in tasks)


def upkeep_start(tasks: list[tuple[str, str]], first_revision: int) -> str:
    """Return the notice naming startable tasks as ``title (node_id)`` and the exact start call.

    ``first_revision`` is the first task's revision: ``start`` requires it.
    """
    return f"{UPKEEP_LABEL} " + _UPKEEP_START.format(
        tasks=_named_tasks(tasks), first_id=tasks[0][1], first_revision=first_revision,
    )


def upkeep_waiting(tasks: list[tuple[str, str]]) -> str:
    """Return the notice naming waiting tasks, so new work is added only when it belongs to none."""
    return f"{UPKEEP_LABEL} " + _UPKEEP_WAITING.format(tasks=_named_tasks(tasks))


def upkeep_active(title: str) -> str:
    """Return the notice naming the thread's selected task as active."""
    return f"{UPKEEP_LABEL} " + _UPKEEP_ACTIVE.format(title=title)


def upkeep_held(title: str, wait: str) -> str:
    """Return the notice naming the task the thread set aside, with its real hold."""
    return f"{UPKEEP_LABEL} " + _UPKEEP_HELD.format(title=title, wait=wait)


def condition_text(condition: str, reason: str) -> str:
    """Return a task's held condition and its reason as one agent-readable line."""
    return f"{condition}: {reason}"


def slot_held_reason(holder_title: str, state: str) -> str:
    """Return the action that starts a named task another task's slot holds up.

    A pending task stays pending, so it needs activating; an active one needs
    starting, once the holder closes.
    """
    action = "activate" if state == "pending" else "start"
    return _SLOT_HELD.format(title=holder_title, action=action, after=_AFTER_CLOSE)


_START_SLOT_HELD = (
    "Not started: {title} ({holder}) holds your executor slot. Settle it, or set "
    "it aside with arm_watch or block_node, and pass next_node_id='{node}' on "
    "that call to start this task in the same write."
)
_MARK_DONE_SLOT_HELD = (
    "mark_done requires active work, and {title} ({holder}) holds your executor "
    "slot, so this task cannot be started yet. Settle that task or set it aside "
    "with next_node_id='{node}', then mark_done this one."
)


_WORKERS_OWN_STEP = (
    "This plan is your worker's, and the worker settles its own steps. Your "
    "own step {title} (plan_id='{plan}', node_id='{step}') carries the "
    "delegation: once the worker hands its work back, accept, reject or fail "
    "that step. To steer the worker meanwhile, use delegate_send."
)
VERDICT_OWED = (
    "You still owe the verdict on {title} ({step}). Its worker returned it and "
    "waits on you; a wait on this step does not settle it. If the work is "
    "verified, accept it: accept_with_follow_up puts what is left (e.g. a merge) "
    "on a follow-up, and the wait goes there. Otherwise reject it with feedback."
)
WORKER_STEP_TO_USER = (
    "A delegated worker cannot assign a step to the user: your parent judges "
    "your work and decides what reaches the user. Put the step up for review "
    "with request_review, or draft a decision with plan_decision, which goes "
    "to your parent first."
)


def workers_own_step(step_title: str, parent_plan_id: str, step_id: str) -> str:
    """Return the refusal for a parent settling a step in its worker's plan (G7)."""
    return _WORKERS_OWN_STEP.format(title=step_title, plan=parent_plan_id, step=step_id)


def start_slot_held(holder_title: str, holder_id: str, node_id: str) -> str:
    """Return why a start or activate left a task unstarted, and how to start it."""
    return _START_SLOT_HELD.format(title=holder_title, holder=holder_id, node=node_id)


def mark_done_slot_held(holder_title: str, holder_id: str, node_id: str) -> str:
    """Return the mark_done refusal for pending work another task's slot holds up."""
    return _MARK_DONE_SLOT_HELD.format(
        title=holder_title, holder=holder_id, node=node_id,
    )


#: ``rank`` on an ``add`` that lands below ready top-level work (T1): where the
#: new task sits, and what runs first. Guidance only; nothing is refused.
TRIAGE_RANK = (
    "{rank} of {total} open. Ready above it: {above}. Work runs in plan order; "
    "reorder it to order_key={key} only if it outranks them."
)
#: ``jumped`` on a named start that passes ready top-level work (T2).
TRIAGE_JUMPED = (
    "Started ahead of ready '{title}' ({node}), ranked {rank} of {total}. If this "
    "outranks it, reorder node_id={top} order_key={key} so the plan says so."
)


def triage_rank(rank: int, total: int, above: list[tuple[str, str]], key: int) -> str:
    """Return the ``rank`` line; ``above`` holds up to two (title, id), topmost first."""
    names = ", ".join(f"'{title}' ({node})" for title, node in above)
    return TRIAGE_RANK.format(rank=rank, total=total, above=names, key=key)


def triage_jumped(
    title: str, node_id: str, rank: int, total: int, top_id: str, key: int,
) -> str:
    """Return the ``jumped`` line for the ready task a start passed over."""
    return TRIAGE_JUMPED.format(
        title=title, node=node_id, rank=rank, total=total, top=top_id, key=key,
    )


def listed_text(shown: int, total: int) -> str:
    """Return how many of the executor's other open tasks a block lists."""
    return _LISTED.format(shown=shown, total=total)


def idle_listed_text(shown: int, total: int, *, set_aside: int = 0) -> str:
    """Return how many of an idle executor's open tasks a block lists."""
    label = _IDLE_LISTED_SET_ASIDE if set_aside else _IDLE_LISTED
    return label.format(shown=shown, total=total)


def is_idle_listed_text(value: str) -> bool:
    """Whether a listing label names either idle-listing order."""
    try:
        shown, total = listed_counts(value)
    except ValueError:
        return False
    return value in (
        idle_listed_text(shown, total), idle_listed_text(shown, total, set_aside=1),
    )


def idle_pick_up(set_aside: int) -> str:
    """Return idle guidance for ordinary waits or work the executor set aside."""
    return (
        _IDLE_PICK_UP_SET_ASIDE.format(set_aside=set_aside)
        if set_aside else IDLE_PICK_UP
    )


def listed_counts(text: str) -> tuple[int, int]:
    """Return the shown and total counts of a ``listed_text`` or ``idle_listed_text`` label.

    Raises:
        ValueError: ``text`` is not a label either produced.
    """
    shown, _of, rest = text.partition(" of ")
    counts = int(shown), int(rest.partition(" ")[0])
    if text not in (
        listed_text(*counts), idle_listed_text(*counts), idle_listed_text(*counts, set_aside=1),
    ):
        raise ValueError("not an open-task listing label")
    return counts


def ends_text(seconds: float) -> str:
    """Return when a wait ends, relative to now, in the coarsest unit that reads exactly enough."""
    if seconds <= 0:
        return "ended; its wake is due"
    minutes = math.ceil(seconds / 60)
    if minutes < 90:
        return f"ends in {minutes} min"
    hours = round(seconds / 3600)
    if hours < 48:
        return f"ends in {hours} h"
    return f"ends in {round(seconds / 86400)} days"


def bounded_text(
    value: str,
    max_bytes: int,
    *,
    measure: Callable[[str], int],
    marker: str = ELLIPSIS,
) -> str:
    """Return ``value`` when it fits ``max_bytes``, else its longest fitting prefix.

    ``measure`` returns the size of a text as the caller emits it. A cut value
    is the longest code-point prefix whose measured size with ``marker``
    appended fits, followed by ``marker``; a code point is never split.

    Raises:
        ValueError: ``max_bytes`` cannot hold ``marker`` alone.
    """
    if measure(value) <= max_bytes:
        return value
    if measure(marker) > max_bytes:
        raise ValueError("max_bytes cannot hold the truncation marker")
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if measure(value[:middle] + marker) <= max_bytes:
            low = middle
        else:
            high = middle - 1
    return value[:low] + marker
