"""Read-only projections over canonical structured-plan state."""

from __future__ import annotations

import hashlib
import json

from infra.packed_rows import fetch_all
from plans import aggregates, node_states, open_tree, wait_causes
from plans.models import PlanLivenessSignal, PlanState, Progress


def progress(plan_id: str, *, db=None) -> Progress:
    """Project descendant-leaf progress and its displayed liveness."""

    return plan_state(plan_id, db=db).progress


def plan_state(
    plan_id: str, *, db=None, memo: dict[str, PlanState] | None = None,
) -> PlanState:
    """Project one plan's progress and whether work in or below it is running.

    A ready leaf whose attached child plan is moving counts as running: its
    owner is not running that delegated step, but the child's plan is doing the
    step's work. The leaf's own review, hold, or wait still wins. A read that
    projects related plans passes one ``memo`` so each plan is walked once.
    """

    from plans import service

    with service._connection(db) as connection:
        return _plan_state(connection, plan_id, {} if memo is None else memo)


#: Marks a plan whose walk is still running, so a plan reached again through a
#: delegated step below it is a cycle rather than a second walk. Never returned.
_WALKING = PlanState(progress=Progress(done=0, total=0, liveness="idle", open=0), moving=False)


def _plan_state(connection, plan_id: str, memo: dict[str, PlanState], *, plan=None, nodes=None, tree=None):
    from plans import admission, service

    walked = memo.get(plan_id)
    if walked is _WALKING:
        raise service.PlanError("plan progress hierarchy contains a cycle")
    if walked is not None:
        return walked
    memo[plan_id] = _WALKING
    if plan is None:
        plan = service._plan_row(connection, plan_id)
    settled_done = settled_failed = 0  # no full rows: the open tree, plus the stored settled leaves
    open_ids: open_tree.UnarchivedIds | None = None
    if nodes is None and aggregates.ready(connection):
        tree = tree or open_tree.load(connection, plan_id)
        rows, counted = open_tree.leaves(tree), open_tree.in_play(tree)
        settled_done, settled_failed = open_tree.settled_leaves_in_play(tree)
        open_ids = open_tree.UnarchivedIds(connection, plan_id, (str(row["id"]) for row in tree.rows))
    else:
        nodes = service._plan_tree_rows(connection, plan_id) if nodes is None else nodes
        rows, counted = node_states.leaves(nodes), node_states.in_play(nodes)
    done = settled_done + sum(row["state"] in service._ACCEPTED_STATES for row in counted)
    incomplete = [row for row in rows if row["state"] not in service._TERMINAL_STATES]
    projected = admission.project_many([(row, plan) for row in incomplete], db=connection, memo=memo)
    executions = [projected[str(row["id"])] for row in incomplete]
    conditions = [execution["condition"] for execution in executions]
    # A step waiting on its delegated child is this plan moving: the child's
    # plan is doing that step's work, and the wait already asked that question.
    # A selected step whose owner is stalled is not running (#3317).
    running = "running" in conditions and not (
        plan["owner_kind"] == "thread" and wait_causes.stalled_why(connection, plan["owner_id"])
    )
    moving = running or any(
        wait_causes.child_is_working(execution) for execution in executions
    )
    state = PlanState(
        progress=Progress(
            done=done, total=len(counted) + settled_done + settled_failed,
            open=sum(row["state"] not in service._TERMINAL_STATES for row in counted),
            liveness=progress_liveness(
                plan, conditions, moving=moving,
                held=_held(connection, plan_id, executions, open_ids),
            ),
        ),
        moving=moving,
    )
    memo[plan_id] = state
    return state


def _held(connection, plan_id: str, executions: list[dict], listed_ids=None) -> bool:
    """Return whether any blocked leaf is blocked by something the user must clear."""
    from plans import operator_node_state

    blocked = [item for item in executions if item["condition"] == "blocked"]
    if not blocked:
        return False
    open_ids = listed_ids if listed_ids is not None else {
        str(row["id"])
        for row in fetch_all(
            connection, "SELECT id FROM plan_nodes WHERE plan_id=? AND archived_at IS NULL", (plan_id,)
        )
    }
    return not all(operator_node_state.quiet(item, open_ids) for item in blocked)


def progress_liveness(plan, conditions: list[str], *, moving: bool, held: bool) -> str:
    """Rank leaf conditions into shared display liveness.

    Only a ``held`` leaf reads blocked; wake and selection use ``liveness_signal``.
    """
    if not conditions:
        return "idle"
    if any(condition == "review" for condition in conditions):
        return "needs-review"
    if moving:
        return "advancing"
    if any(condition == "blocked" for condition in conditions):
        return "blocked" if held else "waiting"
    if all(condition == "waiting" for condition in conditions):
        return "waiting"
    if plan["state"] == "paused" or all(
        condition == "paused" for condition in conditions
    ):
        return "paused"
    return "idle"


def delegated_work_returned(connection, plan_id: str) -> bool:
    """Return whether a delegated plan has returned: steps exist, none still runs.

    A worker returns by putting its last step up for review, failing it, or
    closing it. Every unarchived leaf is then settled or awaiting the parent's
    verdict; a plan whose remaining work was all closed has only closed steps
    left, and that is its return too. A plan that never held a step has
    returned nothing, and a step awaiting its own owner's verdict still runs.
    So does a step that waits on an answer from the user or the parent
    (``delegation_harvest.waiting_on_answer``): the worker acts on the answer
    before the parent judges the work.
    """
    from plans import delegation_harvest

    steps = delegation_harvest.return_rows(connection, plan_id)
    return bool(steps) and not delegation_harvest.waiting_on_answer(connection, plan_id) and all(
        row["state"] not in ("pending", "active")
        and not delegation_harvest.awaiting_owner_verdict(connection, row)
        for row in steps
    )


def open_delegated_node_ids(connection, plan_id: str, *, limit: int = -1) -> tuple[str, ...]:
    """Return up to ``limit`` open step ids with nothing open beneath them.

    The one answer to "does the worker still have work open": an open leaf, or
    a started step (not ``node_states.is_container``) whose own steps have all
    settled, which would otherwise read in progress under finished work. A
    never-started container reads as its steps. Settling one can open its
    parent, so a caller that settles them asks again until none remain.

    A refusal that says only *that* a child plan is unfinished leaves the owner
    hunting for the rows; naming them is what makes it actionable. ``limit``
    (all by default) keeps the mere-existence caller below at ``LIMIT 1``.
    """
    return tuple(
        str(row["id"])
        for row in connection.execute(
            f"""SELECT n.id FROM plan_nodes n
               WHERE n.plan_id=? AND {node_states.in_work_sql('n')}
                 AND n.state IN ('pending','active','review_requested')
                 AND NOT EXISTS (SELECT 1 FROM plan_nodes c
                      WHERE c.parent_id=n.id AND {node_states.in_work_sql('c')}
                        AND c.state IN ('pending','active','review_requested'))
                 AND NOT {node_states.is_container_sql('n')}
               ORDER BY n.order_key,n.created_at
               LIMIT ?""",
            (plan_id, limit),
        )
    )


def open_delegated_work(connection, plan_id: str) -> bool:
    """Return whether a plan still holds open work (``open_delegated_node_ids``)."""

    return bool(open_delegated_node_ids(connection, plan_id, limit=1))


def movable_leaf_sql(alias: str) -> str:
    """SQL conditions for a leaf its plan's owner can still move itself.

    The one definition of "the worker still has work of its own to do": a
    pending or active unarchived leaf that is not the user's and carries no
    open hold. Up-for-review work is not the worker's to move (that is
    ``open_delegated_work``'s wider "unfinished" question). A parent waits on
    a worker only while this holds, and a worker's round is over only when it
    does not.
    """
    return f"""{alias}.archived_at IS NULL
        AND {alias}.state IN ('pending','active')
        AND COALESCE({alias}.assignee,'') <> 'user'
        AND NOT {node_states.has_child_in_work_sql(alias)}
        AND NOT EXISTS (
          SELECT 1 FROM plan_node_holds h
           WHERE h.node_id={alias}.id AND h.cleared_at IS NULL
        )"""


def movable_work(connection, plan_id: str) -> bool:
    """Whether the plan's owner still has a leaf it can move itself."""
    return connection.execute(
        f"SELECT 1 FROM plan_nodes w WHERE w.plan_id=? AND {movable_leaf_sql('w')} LIMIT 1",
        (plan_id,),
    ).fetchone() is not None


def delegated_step(connection, parent_plan_id: str, worker: str):
    """The open step in the parent plan that names this worker, or None.

    The one reading of "this step is still this worker's": unarchived, not
    settled, and attached to exactly this thread. A replaced or released
    worker has none.
    """
    return connection.execute(
        """SELECT id,title,state,spawned_thread_id FROM plan_nodes
           WHERE plan_id=? AND spawned_thread_id=? AND archived_at IS NULL
             AND state IN ('pending','active','review_requested')
           ORDER BY id LIMIT 1""",
        (parent_plan_id, worker),
    ).fetchone()


#: Leaf work this thread could be woken for: its own pending/active tasks, plus
#: any node awaiting review in its plan. Parents derive from children, so a node
#: with live children is never counted itself.
#: ``{open_only}`` is ``AND n.folded=0`` once the aggregate columns exist (older migration rungs run first).
_LIVE_LEAVES_SQL = f"""
    SELECT n.* FROM plan_nodes n
    WHERE n.plan_id=? AND n.archived_at IS NULL{{open_only}}
      AND COALESCE(n.assignee,'') <> 'user'
      AND n.state IN ('pending','active','review_requested')
      AND NOT {node_states.has_child_in_work_sql('n')}
    ORDER BY n.id"""


def live_delegation(connection, worker: str | None, parent_plan_id: str | None) -> bool:
    """Whether a parent plan still holds this worker: spawned by its owner, step open.

    ``parent_plan_id`` is set at attachment and never cleared, so it alone
    says the worker *was* delegated. The delegation is live only while the
    worker is still spawned by the parent plan's owner and an unarchived,
    open delegated step in that plan still names it -- the same shape a
    claim on the parent's routed occurrences checks. After the step is
    archived or the worker detached, the worker's plan is its own again:
    its later reviews are the owner's to give and count as live work.
    """
    if worker is None or parent_plan_id is None:
        return False
    return connection.execute(
        """SELECT 1 FROM plans p
           JOIN threads w ON w.id=? AND w.spawned_by=p.owner_id
           JOIN plan_nodes n ON n.plan_id=p.id AND n.spawned_thread_id=w.id
             AND n.archived_at IS NULL
             AND n.state IN ('pending','active','review_requested')
           WHERE p.id=? AND p.owner_kind='thread' LIMIT 1""",
        (worker, parent_plan_id),
    ).fetchone() is not None


def _is_actionable(row, projection: dict, *, parent_reviews: frozenset[str]) -> bool:
    """Whether a fresh Turn could actually move this node.

    Selected work and review this thread owns always count. Ready work with a
    free executor slot counts too: pending work, and a migrated plan that
    opened no attempt, are real work nothing else would wake.

    A review is the plan owner's to give, except the ones its delegating
    parent judges (``delegation_harvest.parent_reviews``): the worker that
    put those up waits quietly for the parent's verdict. A delegated step
    awaiting the owner's verdict on its child's return is always the owner's.

    Ready work queued behind an occupied executor slot does not count -- the
    task holding that slot is already counted, and reconciliation selects the
    queued one as soon as that slot frees. Work with any cause but a worker's
    unanswered input (a hold, a watch, an unaccepted dependency) never counts,
    review included, so an unchanged wait cannot generate repeated progress wakes.
    """
    if causes := projection["causes"]:
        return all(cause.get("reason_key") == "worker_needs_input" for cause in causes)
    if row["state"] == "review_requested":
        return str(row["id"]) not in parent_reviews
    condition = projection["condition"]
    if condition == "running":
        return True
    return condition == "ready" and projection["conflict"] is None


def _liveness_payload(plan, actionable: list, projected: dict) -> dict:
    """Describe the plan and its actionable nodes for the wake body."""
    return {
        # What the agent is asked to do, not how many times a row was written:
        # its requirements and its instructions. A selection touch or a title
        # edit bumps `revision` without changing the work, and must not look
        # like churn; a rewritten description is new instructions and does.
        "plan": {
            "id": plan["id"],
            "state": plan["state"],
        },
        "nodes": [
            {
                "id": row["id"],
                "requirements_revision": row["requirements_revision"],
                "description": row["description"],
                "state": row["state"],
                "condition": projected[str(row["id"])]["condition"],
            }
            for row in actionable
        ],
    }


def actionable_leaves(connection, plan, *, executor: str | None = None):
    """Return the leaf work a fresh Turn could move now, with its projection.

    One definition of "work this thread could act on", so liveness and the
    plan-wake currency rule cannot answer it differently. ``executor`` narrows
    the leaves to the ones that thread runs -- and to the steps delegated to
    it: a worker handed a step (or its revision) acts on it in its own plan,
    and the wake that hands it over must not be refused for want of work the
    worker has yet to plan. Liveness omits ``executor`` because it already
    reads the thread's own plan, and narrowing there would change what it
    counts.
    """
    import plans.admission_projection as admission_projection
    from plans import admission, delegation_harvest

    sql = _LIVE_LEAVES_SQL.format(open_only=" AND n.folded=0" if aggregates.ready(connection) else "")
    rows = connection.execute(sql, (plan["id"],)).fetchall()
    parent_reviews = delegation_harvest.parent_reviews(connection, plan)
    if executor is not None:
        rows = [
            row for row in rows
            if admission_projection.executor_of(row, plan) == executor
            or row["spawned_thread_id"] == executor
        ]
    projected = admission.project_many(
        [(row, plan) for row in rows],
        db=connection,
    )
    actionable = [
        row for row in rows
        if _is_actionable(row, projected[str(row["id"])], parent_reviews=parent_reviews)
    ]
    return actionable, projected


def liveness_signal(thread_id: str, *, db=None) -> PlanLivenessSignal:
    """Project the execution and review work this thread could act on now."""

    from plans import service

    with service._connection(db) as connection:
        plan = connection.execute(
            """SELECT * FROM plans
               WHERE owner_kind='thread' AND owner_id=? AND state='active'""",
            (thread_id,),
        ).fetchone()
        if plan is None:
            return _signal(None, [], settled_events=0)
        actionable, projected = actionable_leaves(connection, plan)
        settled_events = aggregates.settled_events(connection, plan)
        return _signal(
            plan["id"],
            actionable,
            payload=_liveness_payload(plan, actionable, projected),
            settled_events=settled_events,
        )


def _signal(
    plan_id,
    nodes,
    *,
    payload=None,
    settled_events: int = 0,
) -> PlanLivenessSignal:
    """Build a stable count/hash projection without persisting derived state."""

    normalized = payload or {"plan": None, "nodes": []}
    digest = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return PlanLivenessSignal(
        count=len(nodes),
        signal_hash=digest,
        plan_id=plan_id,
        node_ids=tuple(
            node.id if hasattr(node, "id") else str(node["id"])
            for node in nodes
        ),
        settled_events=settled_events,
    )
