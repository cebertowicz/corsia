"""Which plan task a new delegated worker takes over, and handing it over.

A worker takes over the caller's current task unless the caller names another
task or ``none``, so its result returns through the plan rather than as a bare
chat message.

The task is chosen *before* the worker is spawned, so a request naming a task
that cannot be handed over is refused with nothing started. The hand-over runs
*after*, through the one attach path (``attach_spawned_thread`` behind
``boundary.mutate``), so it is receipted, woken and released exactly as a
hand attach is: the task leaves the caller's slot and the worker's result
returns through it for review.

A worker the caller already started is reused the same way through
``delegate_send``, but only for a task the caller names: a send is often just
a question, so it never takes the caller's current task by default.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from plans import selection_validity, service
from plans.boundary import PlanActor, mutate
from plans.delegation import has_open_structured_attachment
from plans.errors import PlanInputError
from plans.node_states import _TERMINAL_STATES, in_work_sql

#: The ``plan_node_id`` value that keeps the caller's task with the caller.
KEEP = "none"

ATTACHED = (
    "This worker took over your task '{title}' ({node_id}): the task left your"
    " slot, and the worker's result comes back through it for your review. To"
    " keep a task yourself next time, pass plan_node_id='none'."
)
NO_TASK = (
    "Not on your plan: you had no current task, so the result arrives as a"
    " chat message. Pass plan_node_id to hand it one of your tasks."
)
KEPT = (
    "Not on your plan (plan_node_id='none'): you keep your task, and the"
    " result arrives as a chat message."
)
SEND_UNATTACHED = (
    "Not on your plan: this worker has no open task, so its reply arrives as a"
    " chat message. To have it return through a task, pass plan_node_id with"
    " your next send, or plan_update attach_spawned_thread, spawned_thread_id"
    " {child}."
)
NOT_ATTACHED = (
    "The worker did not take over '{title}': {error}. Attach it"
    " yourself with plan_update attach_spawned_thread, spawned_thread_id"
    " {child}, or close the task if it no longer applies."
)


@dataclass(frozen=True)
class Step:
    """The plan task a worker will take over, or already took on a send retry."""

    plan_id: str
    node_id: str
    title: str
    already_attached: bool = False


def kept(requested: Any) -> bool:
    """Whether the caller asked to keep its task (``plan_node_id='none'``)."""
    return isinstance(requested, str) and requested.strip().lower() == KEEP


def choose(parent_thread_id: str, requested: Any, *, db=None) -> Step | None:
    """Return the task the new worker takes over, or None to start it unattached.

    ``requested`` is the caller's ``plan_node_id``: omitted means its current
    task, if it holds one; ``none`` means keep it. A named task that cannot be
    handed over raises ``PlanInputError`` so nothing is spawned.
    """
    if kept(requested):
        return None
    with service._connection(db) as connection:
        return _choose(connection, parent_thread_id, requested)


def _choose(
    db, parent_thread_id: str, requested: Any, *, attached_worker: str | None = None,
) -> Step | None:
    wanted = requested.strip() if isinstance(requested, str) else None
    plan = db.execute(
        "SELECT id FROM plans WHERE owner_kind='thread' AND owner_id=?",
        (parent_thread_id,),
    ).fetchone()
    if wanted:
        node = plan and db.execute(
            "SELECT * FROM plan_nodes WHERE id=? AND plan_id=?", (wanted, plan["id"]),
        ).fetchone()
        if not node:
            raise PlanInputError(
                f"plan_node_id {wanted} is not a task in your plan; pass one of"
                " your task ids, 'none', or omit it for your current task"
            )
        _require_open(node, attached_worker=attached_worker)
        return _step(
            node,
            already_attached=(
                attached_worker is not None
                and node["spawned_thread_id"] == attached_worker
            ),
        )
    if plan is None:
        return None
    current = selection_validity.attributed_selection(db, parent_thread_id)
    if current is None:
        return None
    node = db.execute(
        "SELECT * FROM plan_nodes WHERE id=?", (current["node_id"],),
    ).fetchone()
    return _step(node) if node["spawned_thread_id"] is None else None


def _require_open(node, *, attached_worker: str | None = None) -> None:
    if node["archived_at"] is not None or node["state"] in _TERMINAL_STATES:
        raise PlanInputError(
            f"task {node['id']} is finished; a new worker cannot take it over"
        )
    if (
        node["spawned_thread_id"] is not None
        and node["spawned_thread_id"] != attached_worker
    ):
        raise PlanInputError(
            f"task {node['id']} already has worker {node['spawned_thread_id']};"
            " use plan_update replace_spawned_thread to hand it to another"
        )


def is_doing_open_step(db, thread_id: str, node_id: str) -> bool:
    """Whether ``thread_id`` is the worker on this workflow step, not yet returned or settled."""
    return db.execute(
        "SELECT 1 FROM plan_nodes n WHERE n.id=? AND n.spawned_thread_id=? AND n.workflow_step IS NOT NULL"
        f" AND n.state IN ('pending','active') AND {in_work_sql('n')}",
        (node_id, thread_id),
    ).fetchone() is not None


def _step(node, *, already_attached: bool = False) -> Step:
    return Step(
        str(node["plan_id"]), str(node["id"]), str(node["title"]), already_attached,
    )


def choose_for_send(
    parent_thread_id: str, child_thread_id: str, requested: Any, *, db=None,
) -> Step | None:
    """Return the named task a reused worker takes over, or None to attach nothing.

    Only a task the caller names is handed over, and only to a worker it
    started that holds no other open task; anything else raises
    ``PlanInputError`` so nothing is sent.
    """
    wanted = requested.strip() if isinstance(requested, str) else ""
    if not wanted or kept(wanted):
        return None
    with service._connection(db) as connection:
        child = connection.execute(
            "SELECT spawned_by FROM threads WHERE id=?", (child_thread_id,),
        ).fetchone()
        if child is None or child["spawned_by"] != parent_thread_id:
            raise PlanInputError(
                f"plan_node_id hands a task only to a worker you started;"
                f" {child_thread_id} is not one. Send without plan_node_id"
            )
        step = _choose(
            connection, parent_thread_id, wanted, attached_worker=child_thread_id,
        )
        if step is not None and step.already_attached:
            return step
        if has_open_structured_attachment(child_thread_id, db=connection):
            raise PlanInputError(
                f"worker {child_thread_id} still holds an open task; settle or"
                " close it before handing it another, or send without plan_node_id"
            )
        return step


def send_note(parent_thread_id: str, child_thread_id: str, *, db=None) -> str | None:
    """Say so when a send to the caller's own worker will return as chat."""
    with service._connection(db) as connection:
        child = connection.execute(
            "SELECT spawned_by FROM threads WHERE id=?", (child_thread_id,),
        ).fetchone()
        if child is None or child["spawned_by"] != parent_thread_id:
            return None
        if has_open_structured_attachment(child_thread_id, db=connection):
            return None
        return SEND_UNATTACHED.format(child=child_thread_id)


def hand_over(
    step: Step, child_thread_id: str, parent: dict, *, via: str = "delegate_task", db=None,
):
    """Attach the worker to ``step`` as the caller, through the one attach path."""
    with service._connection(db) as connection:
        return _hand_over(connection, step, child_thread_id, parent, via)


def _hand_over(db, step: Step, child_thread_id: str, parent: dict, via: str):
    revision = db.execute(
        "SELECT revision FROM plan_nodes WHERE id=?", (step.node_id,),
    ).fetchone()["revision"]
    return mutate(
        step.plan_id,
        "attach_spawned_thread",
        {
            "node_id": step.node_id,
            "expected_revision": int(revision),
            "spawned_thread_id": child_thread_id,
        },
        actor=PlanActor(
            thread_id=str(parent["id"]),
            user_id=str(parent["user_id"] or ""),
            project_id=parent.get("project_id"),
        ),
        provenance=f"tool.{via}",
        # A reused worker is handed several tasks over its life: the key names both.
        mutation_id=f"{via}-attach-{child_thread_id}-{step.node_id}",
        db=db,
    )


def plan_task(step: Step | None, *, requested: Any, child_thread_id: str,
              error: str | None = None) -> dict[str, Any]:
    """The ``plan_task`` field of the delegate_task result: what happened and what to do."""
    if step is None:
        return {"attached": False, "note": KEPT if kept(requested) else NO_TASK}
    if error is not None:
        return {
            "attached": False, "node_id": step.node_id,
            "note": NOT_ATTACHED.format(
                title=step.title, error=error, child=child_thread_id,
            ),
        }
    return {
        "attached": True, "node_id": step.node_id, "title": step.title,
        "note": ATTACHED.format(title=step.title, node_id=step.node_id),
    }
