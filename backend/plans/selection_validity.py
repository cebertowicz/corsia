"""The one validity rule for plan selections, read in a fixed number of queries.

``plan_node_attempts`` alone selects work. An open attempt stays its
executor's selection only while it is not superseded - its task still has the
requirements revision and executor it was selected for
(``admission_projection.superseded_by``) - and its task is an unarchived,
childless, active task in an active plan, is not assigned to the user and has
no admission cause. Selection and ``start``, reconciliation, dispatch recording
and settlement, Turn-start accounting and artifact origin all ask this module,
so they cannot disagree. A caller that reports why a task holds no slot asks
``slot_blocker``, which names the first rule the task fails from the same
predicates eligibility is decided by.

The admission projection decides whether a task is running from the same
supersession rule and the same causes. A superseded attempt kept open only
because an already-started call defers its release fails this rule, so no new
call attaches to it, and the projection shows its task waiting on that call.

Every read is batched across the attempts being checked. Dispatch recording
runs this twice on every started tool call, so its cost must not grow with the
number of plans that have selected work for one executor.
"""

from __future__ import annotations

import plans.admission_attempts as attempts
import plans.admission_projection as projection
from plans import node_states

#: Why a task may not take its executor's slot, apart from not being active yet,
#: in the order they are judged and reported: the node is not a live task, the
#: work is the user's, then what keeps the task from being ready.
NOT_TASK = "not_task"
ARCHIVED = "archived"
USER_ASSIGNED = "user_assigned"
PLAN_INACTIVE = "plan_inactive"
HAS_CHILDREN = "has_children"
HELD = "held"
SLOT_BLOCKERS = (NOT_TASK, ARCHIVED, USER_ASSIGNED, PLAN_INACTIVE, HAS_CHILDREN, HELD)


def eligible(db, node, plan) -> bool:
    """Return whether one task may take its executor's slot now."""
    return str(node["id"]) in eligible_ids(db, [(node, plan)])


def slot_blocker(db, node, plan) -> str | None:
    """Return the first ``SLOT_BLOCKERS`` reason a task may not take its slot, or None.

    The task's own lifecycle state is not judged: None means an active task
    would be eligible, and a pending one would be once it is activated.
    """
    return _blockers(db, [(node, plan)])[str(node["id"])]


def slot_blockers(db, node_plans) -> dict[str, str | None]:
    """Return ``slot_blocker`` for many (task, plan) pairs, one batch whatever their number.

    Keyed by task id. Prefer this to calling ``slot_blocker`` in a loop.
    """
    return _blockers(db, list(node_plans))


def row_blocker(node, plan) -> str | None:
    """Return the first reason a task's own row and plan give against selection, or None.

    Reads nothing: children and admission causes are judged by ``slot_blocker``.
    """
    if node["kind"] != "task":
        return NOT_TASK
    if node["archived_at"] is not None:
        return ARCHIVED
    # Work assigned to the user is a human obligation. An agent must never
    # take it into its executor slot: doing so bills the agent's tool cost to
    # the user's task, and -- because the slot is one per plan and executor --
    # queues the agent's own work behind something only the user can settle.
    # `projections.liveness_signal` and the v6->v7 migration already exclude
    # it; this is the same rule at selection.
    if node["assignee"] == "user":
        return USER_ASSIGNED
    if plan["state"] != "active":
        return PLAN_INACTIVE
    return None


def selection_valid(db, attempt) -> bool:
    """Return whether one attempt is still its executor's canonical selection."""
    return bool(valid_selections(db, [attempt]))


def valid_selections(db, rows) -> list:
    """Return the attempt rows that are still valid selections, in their given order."""
    open_rows = [row for row in rows if row["released_at"] is None]
    if not open_rows:
        return []
    nodes, plans = _tasks_and_plans(db, {str(row["node_id"]) for row in open_rows})
    matching = []
    for row in open_rows:
        node = nodes.get(str(row["node_id"]))
        if node is None:
            continue
        plan = plans[str(node["plan_id"])]
        if projection.superseded_by(row, node, plan) is None:
            matching.append((row, node, plan))
    admissible = eligible_ids(db, [(node, plan) for _row, node, plan in matching])
    return [row for row, node, _plan in matching if str(node["id"]) in admissible]


def attributed_selection(db, executor: str):
    """Return the executor's valid selection in its own thread plan, or None.

    Only a plan's owner runs its tasks and each plan holds one open attempt per
    executor, so there is at most one. Turn accounting never takes part.
    """
    valid = valid_selections(db, attempts.current_for_executor(db, executor))
    return valid[0] if valid else None


def _tasks_and_plans(db, node_ids: set[str]) -> tuple[dict, dict]:
    """Read the attempts' task rows and their plans, one query each."""
    node_marks = ",".join("?" for _ in node_ids)
    nodes = {
        str(row["id"]): row
        for row in db.execute(
            f"SELECT * FROM plan_nodes WHERE id IN ({node_marks})",
            tuple(sorted(node_ids)),
        ).fetchall()
    }
    plan_ids = sorted({str(node["plan_id"]) for node in nodes.values()})
    if not plan_ids:
        return nodes, {}
    plan_marks = ",".join("?" for _ in plan_ids)
    plans = {
        str(row["id"]): row
        for row in db.execute(
            f"SELECT * FROM plans WHERE id IN ({plan_marks})", tuple(plan_ids),
        ).fetchall()
    }
    return nodes, plans


def eligible_ids(db, node_plans) -> set[str]:
    """Return the ids of the given (task, plan) pairs that may take their executor's slot now.

    One batch whatever the number of pairs: prefer this to calling
    ``eligible`` in a loop.
    """
    active = [(node, plan) for node, plan in node_plans if node["state"] == "active"]
    return {node_id for node_id, blocker in _blockers(db, active).items() if blocker is None}


def first_eligible_active_id(db, plan) -> str | None:
    """Return the first canonically ordered active task eligible now, if any.

    ``node_order`` owns one SQL cursor and bounds Python eligibility batches;
    this module remains the sole owner of each batch's eligibility predicates.
    """
    from plans import node_order

    for rows in node_order.iter_active_task_pages(db, str(plan["id"])):
        eligible = eligible_ids(db, [(row, plan) for row in rows])
        for row in rows:
            node_id = str(row["id"])
            if node_id in eligible:
                return node_id
    return None


def _blockers(db, node_plans) -> dict[str, str | None]:
    """Return each task's first ``SLOT_BLOCKERS`` reason, or None.

    Row rules are judged first; only tasks that pass them are read for
    unarchived children (one query) and then admission causes (one batch).
    """
    blockers = {str(node["id"]): row_blocker(node, plan) for node, plan in node_plans}
    candidates = [(node, plan) for node, plan in node_plans if blockers[str(node["id"])] is None]
    if not candidates:
        return blockers
    marks = ",".join("?" for _ in candidates)
    parents = node_states.parent_ids(
        db.execute(
            "SELECT id,parent_id,state,archived_at FROM plan_nodes"
            f" WHERE parent_id IN ({marks}) AND {node_states.unarchived_sql('plan_nodes')}",
            tuple(str(node["id"]) for node, _plan in candidates),
        ).fetchall()
    )
    leaves = []
    for node, plan in candidates:
        if str(node["id"]) in parents:
            blockers[str(node["id"])] = HAS_CHILDREN
        else:
            leaves.append((node, plan))
    causes = projection.causes_many(leaves, db=db)
    for node, _plan in leaves:
        if causes[str(node["id"])]:
            blockers[str(node["id"])] = HELD
    return blockers
