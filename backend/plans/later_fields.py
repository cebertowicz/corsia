"""The Later cut on a top-level item: its refusals, and the write every flip makes.

An agent puts work it will not do now below the cut with `later`. The flag is
ranking, not lifecycle: `state` keeps its seven values, and only tasks carry it.
A top-level item is in Later whole or not at all (the person, Nov 24): every
write keeps one value across the item's open tasks, so an edit naming any node
of an item moves all of it, and a step added or moved into an item takes the
item's place. A branch is Later when every open task beneath it is, which the
server rolls up from the tasks, so a stored flag on a branch would be a second
answer to one question.

The reason rides the event and never a column, following the precedent of the
scheduled reason that rides `watch_armed` (`plans.scheduled_wait`): the row shows
the latest one, and a flip that changes nothing writes nothing.
"""

from __future__ import annotations

import sqlite3

from plans import node_states, service

#: The one event every flip writes, with ``{"later": bool, "reason": str|null}``.
EVENT = "node_later"

#: Shown when the cut is asked for where there is no open task to carry it.
LATER_WITHOUT_TASKS = (
    "Later holds an item's open tasks, and this one has none yet. Add its tasks,"
    " then edit it with later."
)
#: Shown when a new step names a side of the cut its item is not on.
LATER_NOT_ITEM = (
    "{title} is {side}, and a step goes where its item is. Leave later out, or"
    " edit {title} with later to move the whole item."
)
#: A reason explains a Later row; on its own it explains nothing.
REASON_WITHOUT_LATER = "reason goes with later: it is why this task is in Later"


def validate_flag(value: object) -> bool:
    """Return the flag as a bool, refusing anything that is not one."""
    if not isinstance(value, bool):
        raise service.PlanInputError("later must be true or false")
    return value


def creation_flag(kind: str, requested: object) -> bool | None:
    """Return the flag a caller asked a new node to be born with, or None when it named none.

    A birth below the cut is not a crossing: there is no earlier state to
    explain, so it asks no reason and writes no event. A new branch has no task
    to carry it yet.
    """
    if requested is None:
        return None
    flag = validate_flag(requested)
    if flag and kind != "task":
        _refuse_without_tasks()
    return flag


def _refuse_without_tasks() -> None:
    raise service.PlanInputError(LATER_WITHOUT_TASKS)


def birth_flag(
    connection, node_id: str, kind: str, parent_id: str | None, requested: bool | None,
) -> bool:
    """Return the flag the new node ``node_id`` is stored with: its item's, when it joins one that has open tasks.

    Only a task stores it; a new branch's Later is its tasks' (``creation_flag``).
    The new node itself is left out of its item's reading, since it is already inserted.

    Raises:
        PlanInputError: The caller named the other side of the cut from its item.
    """
    root_id = item_root_id(connection, parent_id) if parent_id else None
    value = item_value(connection, root_id, exclude=node_id) if root_id else None
    if root_id and value is not None and requested is not None and requested != value:
        root = service._node_row(connection, root_id)
        raise service.PlanInputError(LATER_NOT_ITEM.format(
            title=root["title"], side="in Later" if value else "not in Later",
        ))
    flag = value if value is not None else bool(requested)
    return flag and kind == "task"


def item_root_id(connection, node_id: str) -> str:
    """The id of the top-level item ``node_id`` belongs to (itself when it is top-level)."""
    row = connection.execute(
        "WITH RECURSIVE up(id, parent_id) AS ("
        " SELECT id, parent_id FROM plan_nodes WHERE id=?"
        " UNION ALL SELECT p.id, p.parent_id FROM plan_nodes p JOIN up ON p.id=up.parent_id)"
        " SELECT id FROM up WHERE parent_id IS NULL",
        (node_id,),
    ).fetchone()
    return str(row["id"]) if row else node_id


def item_tasks(connection, root_id: str, *, exclude: str | None = None) -> list:
    """The open tasks of the item under ``root_id``, leaving out the subtree at ``exclude``."""
    return connection.execute(
        "WITH RECURSIVE down(id, plan_id) AS ("
        f" SELECT n.id, n.plan_id FROM plan_nodes n WHERE n.id=? AND n.id IS NOT ? AND {node_states.in_work_sql('n')}"
        " UNION ALL SELECT c.id, c.plan_id FROM down"
        " CROSS JOIN plan_nodes c ON c.plan_id=down.plan_id AND c.parent_id=down.id"  # idx_plan_nodes_tree
        f" WHERE c.id IS NOT ? AND {node_states.in_work_sql('c')})"
        " SELECT n.* FROM down CROSS JOIN plan_nodes n ON n.id=down.id"
        f" WHERE n.kind='task' AND {node_states.open_sql('n')}",
        (root_id, exclude, exclude),
    ).fetchall()


def item_task_ids(connection, node_ids) -> dict[str, str]:
    """Each open task of the items the named nodes belong to, mapped to the named node."""
    owners: dict[str, str] = {}
    for node_id in node_ids:
        for row in item_tasks(connection, item_root_id(connection, node_id)):
            owners.setdefault(str(row["id"]), node_id)
    return owners


def item_value(connection, root_id: str, *, exclude: str | None = None) -> bool | None:
    """Whether the item is in Later, or None when it has no open task to say.

    An item is in Later only when all of its open tasks are, so an item that
    predates the whole-item rule reads as not in Later until it is normalized.
    """
    rows = item_tasks(connection, root_id, exclude=exclude)
    if not rows:
        return None
    return all(bool(row["later"]) for row in rows)


def set_item(
    connection, node_id: str, *, later: bool, reason: str | None, actor_id: str | None,
    provenance: str, skip: frozenset[str] = frozenset(),
) -> list[str]:
    """Put every open task of ``node_id``'s item on one side of the cut; return the tasks flipped."""
    flipped = []
    for row in item_tasks(connection, item_root_id(connection, node_id)):
        task_id = str(row["id"])
        if task_id in skip or bool(row["later"]) == later:
            continue
        flip(connection, task_id, later=later, reason=reason, actor_id=actor_id, provenance=provenance)
        flipped.append(task_id)
    return flipped


def item_crosses(connection, node, later: bool) -> bool:
    """Whether an edit naming ``later`` on ``node`` moves any open task of its item."""
    return any(bool(row["later"]) != later for row in item_tasks(connection, item_root_id(connection, node["id"])))


def crossing(connection, node, later: bool | None, *, actor_id: str | None) -> tuple[bool, bool]:
    """Check an edit that may name the cut; say whether the node's own column and its item move.

    The node's own column rides the edit's one write; the rest of its item follows in ``after_edit``.
    """
    if later is None:
        return False, False
    validate_later_edit(connection, node, later, actor_id=actor_id)
    return node["kind"] == "task" and later != bool(node["later"]), item_crosses(connection, node, later)


def after_edit(
    connection, node_id: str, later: bool | None, moved: bool, *,
    reason: str | None, actor_id: str | None, provenance: str,
) -> None:
    """Keep the edited node's item on one side: the side the edit named, or after a move the item's own."""
    if later is not None:
        set_item(connection, node_id, later=later, reason=reason, actor_id=actor_id,
                 provenance=provenance, skip=frozenset({node_id}))
    elif moved:
        join_item(connection, node_id, actor_id=actor_id, provenance=provenance)


def join_item(connection, node_id: str, *, actor_id: str | None, provenance: str) -> None:
    """After ``node_id`` moved under another node, give its tasks the item's place.

    A running row never sits inside Later (Later design, 2026-11-07), so moving
    started work into an item in Later brings the item back.
    """
    import plans.admission_attempts as attempts

    root = item_root_id(connection, node_id)
    if root == node_id:
        return
    value = item_value(connection, root, exclude=node_id)
    if value is None:
        return
    moved = [str(row["id"]) for row in item_tasks(connection, node_id)]
    if value and attempts.open_attempts(moved, db=connection, compact=True):
        value = False
    set_item(connection, node_id, later=value, reason=None, actor_id=actor_id, provenance=provenance)


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

    if later:
        # A plan reviews its pile one week after the first task crossed the cut;
        # a crossing back above it moves nothing (``plans.later_review``).
        from plans import later_review

        later_review.arm_node(connection, node_id)


def latest_reasons(db, nodes) -> dict[str, str]:
    """The reason each Later task's row shows: the one its latest crossing recorded, by node id.

    Reads only tasks in Later, so a plan with none costs no query. A task whose
    latest crossing gave no reason is absent.
    """
    import json

    from plans.operator_node_facts import id_chunks

    ids = sorted(str(node["id"]) for node in nodes if node["kind"] == "task" and node.get("later"))
    reasons: dict[str, str] = {}
    for chunk in [part for part in id_chunks(ids) if part]:
        marks = ",".join("?" for _ in chunk)
        rows = db.execute(
            f"""SELECT node_id, payload_json FROM (
                  SELECT node_id, payload_json,
                         ROW_NUMBER() OVER (PARTITION BY node_id ORDER BY created_at DESC, rowid DESC) AS latest
                  FROM plan_node_events INDEXED BY idx_plan_node_events_node
                  WHERE node_id IN ({marks}) AND event=?
                ) WHERE latest=1""",
            (*chunk, EVENT),
        ).fetchall()
        for row in rows:
            reason = json.loads(row["payload_json"] or "{}").get("reason")
            if reason:
                reasons[str(row["node_id"])] = str(reason)
    return reasons


def validate_later_edit(connection, node, later: object, *, actor_id: str | None) -> None:
    """Check an edit that names the cut before anything is written.

    The edit moves the node's whole item, so it needs an open task there, and a
    running row never sits inside Later (Later design, 2026-11-07): an agent
    moving delegated work below the cut stops its helper first.
    """
    rows = item_tasks(connection, item_root_id(connection, node["id"]))
    if not rows:
        _refuse_without_tasks()
    for row in rows:
        if later and not row["later"]:
            _refuse_live_helper(connection, row, actor_id=actor_id)


def _refuse_live_helper(connection, node, *, actor_id: str | None) -> None:
    """Refuse an agent's move while the canonical delegated wait is still live."""
    from plans import wait_causes
    from plans.boundary import is_user_audit_id
    from prompts.plan_later_helper import HELPER_STILL_ON_TASK

    if is_user_audit_id(actor_id) or not node["spawned_thread_id"]:
        return
    plan = service._plan_row(connection, node["plan_id"])
    causes = wait_causes.delegation_causes([(node, plan)], db=connection).get(str(node["id"]), [])
    for cause in causes:
        if cause["reason_key"] != wait_causes.DELEGATED_CHILD_UNAVAILABLE:
            raise service.PlanInputError(HELPER_STILL_ON_TASK.format(
                thread=cause["child_thread_id"], title=node["title"],
            ))


def edit_across_cut(params: dict, changed: dict, common: dict, acting: str | None) -> dict:
    """Apply an edit that names the cut, and free the slot when the cut moved onto it.

    A pull back applies the cap on unstarted work (``plans.plan_cap``) after it.

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
    # The edit moves the whole item, so the slot it frees is whichever task of the item holds one.
    held = [task_id for task_id in item_task_ids(db, [node_id]) if attempts.open_attempt(db, task_id) is not None]
    slot = close_next.slot_before(db, service._node_row(db, held[0] if held else node_id))
    mutations.edit_node(node_id, **changed, **expected(params), **common)
    if changed["later"]:
        for task_id in held:
            admission.release(task_id, "later", db=db)
    if not changed["later"]:
        # A pull back stays where it was put; the lowest other unstarted item makes room,
        # and the answer names what moved, as an add's landing does.
        from plans import plan_cap
        from prompts import plan_top as top_text

        moved = plan_cap.pulled_back(db, [node_id], actor_id=common["actor_id"])
        result = admission.result(node_id, db=db)
        if moved:
            result["landing"] = top_text.top_made_room(
                [str(service._node_row(db, root)["title"]) for root in moved], cap=plan_cap.CAP,
            )
        return result
    if not held or any(attempts.open_attempt(db, task_id) for task_id in held):
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
