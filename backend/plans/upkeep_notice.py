"""The one-line plan-upkeep notice beside a Turn's first tool result.

Agents forget to move their plan when work finishes, stalls, or changes. The
first tool result of a Turn carries one short line about the thread's current
plan task, so the agent sees its plan state without reading the plan:

- a selected task is named as active, with the updates that apply;
- a task the agent set aside on a real hold is named with that hold, so the
  agent keeps it held instead of clearing the wait to quiet the notice;
- with nothing selected, the agent is told the exact call that starts one of its
  startable tasks, named by title and id, or how to add one when none can start.

Plans supplies the facts here and ``prompts.plan_tools`` the words. The delivery
layer attaches the line beside the model's projection of the result; the
persisted result, its hash and exact retrieval are never changed. Results of the
Plans tools themselves carry no notice: that agent is already working its plan.
"""

from __future__ import annotations

from plans import (
    admission, admission_projection, node_order, node_states, selection_validity, service,
)
from prompts import plan_tools as text

#: Serialized-byte ceiling for the whole notice line.
NOTICE_MAX_BYTES = 320
TITLE_MAX_BYTES = 96
#: Title cut in the no-selection line, which also carries two 32-character ids
#: and the start call inside the whole-line cap.
START_TITLE_MAX_BYTES = 40
START_OFFER_MAX = 2
WAIT_MAX_BYTES = 72
_UNSETTLED_STATES = frozenset({"pending", "active", "review_requested"})
_HELD_CONDITIONS = frozenset({"paused", "blocked", "waiting"})


def is_plans_tool(tool_name: str) -> bool:
    """Return whether a dispatched tool is one of Plans' own (its result carries no notice).

    Delegates to the tool catalog's classifier, resolved at call time because
    the tool layer imports Plans.
    """
    from tools.dispatch_ownership import is_plans_catalog_tool

    return is_plans_catalog_tool(tool_name)


def notice_for(db, thread_id: str, tool_name: str) -> str | None:
    """Return the plan-upkeep line for this thread's current plan task, or None."""
    if is_plans_tool(tool_name):
        return None
    plan = db.execute(
        "SELECT * FROM plans WHERE owner_kind='thread' AND owner_id=? AND state='active'",
        (thread_id,),
    ).fetchone()
    if plan is None:
        return None
    attempt = selection_validity.attributed_selection(db, thread_id)
    if attempt is not None:
        title = service._node_row(db, attempt["node_id"])["title"]
        return _fit(text.upkeep_active(_cut(title, TITLE_MAX_BYTES)))
    held = _held_task(db, plan, thread_id)
    if held is not None:
        title, wait = held
        return _fit(text.upkeep_held(_cut(title, TITLE_MAX_BYTES), _cut(wait, WAIT_MAX_BYTES)))
    startable, waiting = _open_tasks(db, plan, thread_id)
    if startable:
        return _fit(text.upkeep_start(_named(startable), int(startable[0]["revision"])))
    if not waiting:
        waiting = _unsettled_tasks(db, plan, thread_id)
    if waiting:
        return _fit(text.upkeep_waiting(_named(waiting)))
    return text.UPKEEP_ADD_ROUTE


def _unsettled_tasks(db, plan, thread_id: str) -> list:
    """Return this executor's unsettled tasks that ``open_tasks`` leaves out, in plan order.

    Work awaiting review (a parent's delegated step) and user-assigned work are
    not open, but the plan still tracks them, so the add route must not fire
    while one exists.
    """
    rows = node_order.ordered_cursor(db, plan["id"]).fetchall()
    parents = node_states.parent_ids(rows)
    return [
        row for row in rows
        if row["kind"] == "task" and row["state"] in _UNSETTLED_STATES
        and row["id"] not in parents
        and admission_projection.executor_of(row, plan) == thread_id
    ]


def _open_tasks(db, plan, thread_id: str) -> tuple[list, list]:
    """Return this executor's open tasks split into startable and waiting, in plan order.

    A held or waiting task is still the executor's work, so it stays open, but
    it is not something the agent can start: telling it to start one would ask
    it to clear a real wait just to quiet this line. Naming it instead keeps the
    agent from adding a duplicate of work that already has a task.
    """
    rows = node_order.open_tasks(db, plan, thread_id)
    if not rows:
        return [], []
    admitted = admission.project_many([(row, plan) for row in rows], db=db)
    startable = [row for row in rows if not admitted[str(row["id"])]["causes"]]
    waiting = [row for row in rows if admitted[str(row["id"])]["causes"]]
    return startable, waiting


def _named(rows: list) -> list[tuple[str, str]]:
    return [
        (_cut(str(row["title"]), START_TITLE_MAX_BYTES), str(row["id"]))
        for row in rows[:START_OFFER_MAX]
    ]


def _held_task(db, plan, thread_id: str) -> tuple[str, str] | None:
    """Return the title and hold of the task this executor last set aside, if still held."""
    row = db.execute(
        """SELECT a.node_id FROM plan_node_attempts a
           JOIN plan_nodes n ON n.id=a.node_id
           WHERE a.plan_id=? AND a.executor_thread_id=? AND a.released_at IS NOT NULL
             AND n.state='active' AND n.archived_at IS NULL
           ORDER BY a.released_at DESC LIMIT 1""",
        (plan["id"], thread_id),
    ).fetchone()
    if row is None:
        return None
    node = service._node_row(db, row["node_id"])
    projection = admission.project_many([(node, plan)], db=db)[str(node["id"])]
    if projection["condition"] not in _HELD_CONDITIONS:
        return None
    wait = text.condition_text(projection["condition"], projection["primary_reason"])
    return str(node["title"]), wait


def _cut(value: str, max_bytes: int) -> str:
    return text.bounded_text(value, max_bytes, measure=_utf8_bytes)


def _fit(line: str) -> str:
    return text.bounded_text(line, NOTICE_MAX_BYTES, measure=_utf8_bytes)


def _utf8_bytes(value: str) -> int:
    return len(value.encode("utf-8"))
