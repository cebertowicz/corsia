"""What a handoff says about the task itself, beside its ``next`` block.

The ``next`` block names the task a thread now holds and spends its bytes on
what every handoff needs. A task somebody else wrote needs more: its executor
did not write it, so a task whose whole content is its description (a
document-feedback task's items) would otherwise arrive as a title and a read
still to make.

Both ways a task reaches its executor, the work-selected wake and the result
of the close that selects it, ask this module the same questions, so they
cannot answer them differently: whether the task's description is carried, and
how a carried field is bounded. A handoff field is whole up to a ceiling and
past it names the read that returns the rest. One writer builds every such
field, so the cut is made and worded once, wherever the handoff is composed.

Every handoff of such a task carries its description, a task set aside and
taken back included. Nothing records what was carried, so nothing can claim
the executor saw a text it never read; the cost is the description again each
time the task returns.
"""

from __future__ import annotations

import functools

#: The fact beside ``next`` that carries the selected task's description.
RESULT_KEY = "selected_description"


def bounded_field(db, node, own_plan_id: str):
    """Return the writer of one handoff field about ``node``.

    ``own_plan_id`` is the plan of the thread the handoff goes to: a task held
    in another thread's plan names that thread in its cut, so the cut names a
    read its holder can make.
    """
    from plans import service
    from plans.close_next_bounds import json_bytes
    from prompts import plan_tools as text

    node_id = str(node["id"])
    held = service._plan_row(db, node["plan_id"])
    foreign = str(held["id"]) != own_plan_id and held["owner_kind"] == "thread"
    return functools.partial(
        text.revision_field, measure=json_bytes, node_id=node_id,
        plan_thread=str(held["owner_id"]) if foreign else None,
    )


def written_by_another(db, node_id: str, executor: str) -> bool:
    """Whether someone other than its executor created this task.

    An executor wrote its own tasks and needs no copy of them. The author is
    the actor of the task's ``node_created`` event, and an event that records
    no actor is another author: a trusted writer's, as a document-feedback or
    lesson-review task is created. Only a task with no creation event has no
    known author, and that is not evidence of another one.
    """
    created = db.execute(
        "SELECT actor_id FROM plan_node_events WHERE node_id=? AND event='node_created' "
        "ORDER BY created_at,id LIMIT 1",
        (node_id,),
    ).fetchone()
    return created is not None and created["actor_id"] != executor


def owed(db, node, executor: str) -> bool:
    """Whether a handoff of this task owes its executor the description.

    Owed when the task has a description and someone else created it, on
    every handoff: one definition for both routes.
    """
    description = node["description"]
    return bool(description) and written_by_another(db, str(node["id"]), executor)


def fact(db, node_id: str, executor: str) -> dict:
    """Return the description fact one handoff of a task carries.

    Empty when the handoff owes no description (``owed``), so a caller adds
    what this returns and the key is absent rather than null. ``cut`` is the
    writer's own answer to whether the text was cut at its ceiling: a field
    that differs from the stored description is a cut one. Resolves the
    executor's own thread plan through ``service.ensure_plan``, which creates
    that plan when it does not exist.
    """
    from plans import service
    from plans.close_next_bounds import REVISION_BRIEF_MAX_BYTES as cap

    node = service._node_row(db, node_id)
    if not owed(db, node, executor):
        return {}
    description = str(node["description"])
    own_plan = service.ensure_plan("thread", executor, db=db)
    text = bounded_field(db, node, own_plan.id)(description, cap, what="description")
    return {RESULT_KEY: {"node_id": node_id, "text": text, "cut": text != description}}


def measured(result: dict) -> dict:
    """Return what a closing result's description cost, from the fact it carries.

    Read from the fact the writer built, never from storage: whether one was
    carried, its serialized bytes, and the writer's cut flag. Nothing of the
    text leaves here.
    """
    from plans.close_next_bounds import json_bytes

    carried = result.get(RESULT_KEY)
    if carried is None:
        return {"description_carried": False, "description_bytes": 0, "description_cut": False}
    return {
        "description_carried": True,
        "description_bytes": json_bytes(carried["text"]),
        "description_cut": carried["cut"],
    }
