"""Batched readers for the waits a task is in: an open decision, a working child.

A wait is not a second kind of state. It is one more admission cause, composed
by ``admission_projection._node_causes`` beside holds, dependencies and armed
watches, so everything that already reads causes -- eligibility, the operator
display, liveness and the watchdog -- sees a waiting task with no new store and
no second owner.

Each reader answers for a whole batch with one query per kind, so a projection's
query count stays bounded by the kinds of wait, not by the size of the batch.

Three waits live here:

- **A decision on the task.** A raised decision waits for the user's answer; a
  draft or parent-reviewed one waits for the parent tier's review. Root work has
  no higher tier, so its author must act on its own draft and that is no wait.
- **A delegated child still working.** A parent does not run the step its child
  is doing, and a child thread that is gone blocks that step instead. "Still
  working" is the child's own plan moving (``PlanState.moving``, the fact the
  operator display reads), a Turn it is running, an input queued for its next
  Turn (a rejected step's revision handed back to it, #2052), or a step it can
  still move (``projections.movable_leaf_sql``, the one definition), so a child
  sent back by message waits for its return (matrix row D4). A paused child is
  not working: its step reads paused (pause contract, #1970). Nor is a stalled
  child (``stalled_sql``): its step waits, naming why (#3317).
- **A delegated child needing parent input** (``plans.delegation_needs``).
"""

from __future__ import annotations

from contextlib import contextmanager


@contextmanager
def account_read_snapshot(db):
    """The authorized Plans read owns kind and advisory readiness in one snapshot."""
    db.execute('SAVEPOINT plan_account_read')
    try:
        yield
    finally:
        db.execute('RELEASE plan_account_read')

DECISION_RAISED = "decision_raised"
DECISION_REVIEW = "decision_review"
DELEGATED_CHILD = "delegated_child"
DELEGATED_CHILD_REVIEW = "delegated_child_review"
DELEGATED_CHILD_UNAVAILABLE = "delegated_child_unavailable"
DELEGATED_CHILD_PAUSED = "delegated_child_paused"

_STALLED = {
    "model": "The delegated child is stalled: its model is unavailable",
    "account": "The delegated child is stalled: its input is held for its account",
    "error": "The delegated child is stalled: its last Turn failed",
}


def stalled_sql(thread: str) -> str:
    """SQL naming why ``thread`` cannot move its plan now, else NULL (#3317).

    ``'model'``: its FIFO head is held for an unavailable model. ``'error'``:
    its last Turn failed and nothing runnable is queued. Either way no Turn
    runs. ``thread`` is a column or a named parameter, never caller text.
    """
    from infra.unavailable_model_recovery import held_head_kind_sql

    return f"""CASE
      WHEN EXISTS (SELECT 1 FROM active_turns AS live WHERE live.thread_id={thread}
                     AND live.status IN ('running','waiting_retry')) THEN NULL
      WHEN ({held_head_kind_sql(thread)}) IS NOT NULL THEN {held_head_kind_sql(thread)}
      WHEN (SELECT last.status FROM active_turns AS last WHERE last.thread_id={thread}
             ORDER BY last.started_at DESC, last.rowid DESC LIMIT 1)='error'
       AND NOT EXISTS (SELECT 1 FROM messages AS queued
             WHERE queued.thread_id={thread} AND queued.input_order IS NOT NULL
               AND queued.delivery_state='queued') THEN 'error'
    END"""


def stalled_why(db, thread_id: str) -> str | None:
    """Return why one thread is stalled (``stalled_sql``), or None."""
    row = db.execute(
        f"SELECT {stalled_sql(':thread')} AS why", {"thread": thread_id},
    ).fetchone()
    return None if row is None else row["why"]

#: Every delegation fact, read from the nodes themselves: callers project nodes
#: with different column sets, and a wait may not be derived from whichever
#: columns one of them happened to select.
_CHILD_SQL = """
    WITH delegating AS (
      SELECT n.id AS node_id, c.id AS plan_id FROM plan_nodes n LEFT JOIN plans c ON {edge}
       WHERE n.id IN ({marks}) AND n.spawned_thread_id IS NOT NULL AND {live}
         AND COALESCE({newest_attachment}, n.id)=n.id
    ), open_needs AS ({open_needs})
    SELECT n.id AS node_id, n.spawned_thread_id AS child_id,
           n.executor_thread_id AS executor_thread_id,
           n.owner_thread_id AS owner_thread_id,
           t.lifecycle_state AS lifecycle_state, t.archived_reason AS archived_reason,
           c.id AS child_plan_id, c.state AS child_plan_state,
           (open_needs.plan_id IS NOT NULL) AS parent_need, open_needs.reason AS parent_need_reason,
           EXISTS (SELECT 1 FROM active_turns a
              WHERE a.thread_id=n.spawned_thread_id AND a.status='running') AS turn_running,
           EXISTS (
             SELECT 1 FROM active_turns a
              WHERE a.thread_id=n.spawned_thread_id AND a.status='running'
                AND julianday(a.started_at) > julianday((
                  SELECT MAX(e.created_at) FROM plan_node_events e
                  JOIN plan_nodes reviewed ON reviewed.id=e.node_id
                  WHERE reviewed.plan_id=c.id AND e.event='state_transition'
                    AND e.new_state='review_requested'
                ))
           ) AS turn_started_after_review,
           EXISTS (SELECT 1 FROM messages m
              WHERE m.thread_id=n.spawned_thread_id AND m.input_order IS NOT NULL
                AND m.delivery_state='queued') AS input_queued,
           {stalled} AS stalled,
           EXISTS (SELECT 1 FROM plan_nodes w WHERE w.plan_id=c.id AND {movable}) AS open_work,
           EXISTS (
             SELECT 1 FROM plan_nodes w
              WHERE w.plan_id=c.id AND w.state='active' AND {movable}
           ) AS active_work,
           (SELECT MAX(e.created_at) FROM plan_node_events e
             WHERE e.node_id=n.id AND e.event='spawned_thread_attached') AS attached_at
      FROM delegating
      JOIN plan_nodes n ON n.id=delegating.node_id
      LEFT JOIN threads t ON t.id=n.spawned_thread_id
      LEFT JOIN plans c ON c.id=delegating.plan_id
      LEFT JOIN open_needs ON open_needs.plan_id=c.id
"""


def working_child(projection: dict) -> dict | None:
    """Return the working delegated child's cause if its work is what a step waits on.

    One predicate for every reader: the operator display and the status read
    show such a step in progress, and its plan counts as moving. A hold, a
    dependency or a decision on the step is named first and wins, so a paused
    step never reads as progress just because its child is busy. A watch does
    not: it is the parent's check-in on the child, not a stop (#2373), unless
    it is a blocker armed as a wait (``plans.blocked_wait``), which wins like
    the hold it replaced. A child between Turns with steps still to take up
    holds the step (it waits) but is not progress (``child_active``).
    """
    for cause in projection["causes"]:
        if cause.get("watch_id") is not None and not cause.get("blocked_wait"):
            continue
        if cause["reason_key"] == DELEGATED_CHILD and cause.get("child_active", True):
            return cause
        return None
    return None


def child_is_working(projection: dict) -> bool:
    """Return whether a delegated child is doing a step's work (``working_child``)."""
    return working_child(projection) is not None


def paused_since(node_plans, *, db) -> dict[str, str]:
    """Return each paused plan's own pause time, read from its event log.

    ``plans.updated_at`` is touched by unrelated later writes (a decision
    raised, a reparent) while a plan stays paused, so it cannot answer "since
    when" -- the plan's own append-only ``state_transition`` event can.
    """
    plan_ids = sorted({
        str(plan["id"]) for _node, plan in node_plans if plan["state"] == "paused"
    })
    if not plan_ids:
        return {}
    marks = ",".join("?" for _ in plan_ids)
    rows = db.execute(
        f"""SELECT plan_id, MAX(created_at) AS paused_at FROM plan_events
            WHERE plan_id IN ({marks}) AND event='state_transition'
              AND new_state='paused'
            GROUP BY plan_id""",
        tuple(plan_ids),
    ).fetchall()
    return {str(row["plan_id"]): row["paused_at"] for row in rows}


def decision_causes(node_plans, *, db) -> dict[str, list[dict]]:
    """Return the open-decision wait of each node in the batch that has one."""
    from plans import decision_projection

    sources = {str(node["id"]): plan for node, plan in node_plans}
    if not sources:
        return {}
    plan_ids = {str(plan["id"]) for plan in sources.values()}
    open_rows = [
        row
        for row in decision_projection.rows_for_plans(plan_ids, db=db, open_nodes=True)
        if row["state"] in decision_projection.OPEN_STATES
        and row["source_node_id"] in sources
    ]
    if not open_rows:
        return {}
    # A raised decision is a wait only while the user can actually answer it.
    # `raised_decisions` is that rule: the body, the attention pile and the
    # answer route must agree, so a question cleared out of band stops holding
    # its task. Read once for the batch.
    answerable = {
        row["id"]
        for row in decision_projection.raised_decisions(plan_ids, db=db, open_nodes=True)
    }
    reviewers = _reviewers(
        db, {str(sources[row["source_node_id"]]["owner_id"]) for row in open_rows},
    )
    causes: dict[str, list[dict]] = {}
    for row in open_rows:
        author = str(sources[row["source_node_id"]]["owner_id"])
        cause = _decision_cause(
            row, author=author, reviewer=reviewers.get(author),
            answerable=row["id"] in answerable,
        )
        if cause is not None:
            causes.setdefault(str(row["source_node_id"]), []).append(cause)
    return causes


def decision_wait_kind(
    state: str, *, author: str, reviewer: str | None, answerable: bool,
) -> str | None:
    """The shared raised/draft parent-tier wait classification."""
    from plans import decision_projection

    if state == decision_projection.RAISED:
        return DECISION_RAISED if answerable else None
    if state not in decision_projection.OPEN_STATES or reviewer is None or reviewer == author:
        return None
    return DECISION_REVIEW


def _decision_cause(
    row: dict, *, author: str, reviewer: str | None, answerable: bool,
) -> dict | None:
    """Return the wait one open decision places on its source task, if any."""
    kind = decision_wait_kind(row["state"], author=author, reviewer=reviewer, answerable=answerable)
    if kind is None:
        return None
    if kind == DECISION_RAISED:
        return {
            "kind": "waiting",
            "reason_key": DECISION_RAISED,
            "reason": "Waiting for the user to answer a decision on this task",
            "owner_kind": "user",
            "owner_id": None,
            "decision_id": row["id"],
            "created_at": row["raised_at"],
        }
    return {
        "kind": "waiting",
        "reason_key": DECISION_REVIEW,
        "reason": "Waiting for the parent to review a decision on this task",
        "owner_kind": "thread",
        "owner_id": reviewer,
        "decision_id": row["id"],
        "created_at": row["created_at"],
    }


def _reviewers(db, authors: set[str]) -> dict[str, str | None]:
    """Return each author thread's parent review tier in one read."""
    if not authors:
        return {}
    marks = ",".join("?" for _ in authors)
    rows = db.execute(
        f"SELECT id, spawned_by FROM threads WHERE id IN ({marks})",
        tuple(sorted(authors)),
    ).fetchall()
    return {str(row["id"]): row["spawned_by"] for row in rows}


def delegation_facts(node_plans, *, db) -> dict[str, dict]:
    """Read the canonical delegated-child facts through one set selector.

    A reused worker belongs only to its newest attachment, the same ownership
    rule the full tree uses when nesting the child summary.
    """
    from plans import delegation, delegation_needs, hierarchy, node_waits, projections
    node_ids = [str(node["id"]) for node, _plan in node_plans]
    if not node_ids:
        return {}
    live, states = delegation._live_attachment(alias="n")
    sql = _CHILD_SQL.format(
        marks="SELECT value FROM json_each(?1)",  # ?2... bind the states of ``live``
        open_needs=delegation_needs.open_needs_by_plan_sql("SELECT plan_id FROM delegating"),
        edge=hierarchy.delegation_edge_sql("n", "c"),
        live=live,
        movable=projections.movable_leaf_sql("w"),
        stalled=stalled_sql("n.spawned_thread_id"),
        newest_attachment=hierarchy.newest_attachment_sql("c"),
    )
    from connections import read_account_readiness
    from infra.packed_rows import fetch_all
    with account_read_snapshot(db):
        rows = fetch_all(db, sql, (node_waits.encoded_ids(node_ids), *states))
        readiness = read_account_readiness(
            [str(row['child_id']) for row in rows if row['stalled'] == 'account'], db=db)
    return {str(row['node_id']): dict(row) | {'account_readiness': readiness.get(str(row['child_id']))} for row in rows}


def delegation_causes(node_plans, *, db, memo=None) -> dict[str, list[dict]]:
    """Return the delegated-child wait of each node whose child is still on it."""
    from plans import projections

    pairs = list(node_plans)
    plans_by_node = {str(node["id"]): plan for node, plan in pairs}
    memo = {} if memo is None else memo
    causes: dict[str, list[dict]] = {}
    for node_id, row in delegation_facts(pairs, db=db).items():
        # No child plan under this node is an attachment that never completed:
        # there is nothing running below, so the step is the parent's own work.
        child_plan_id = row["child_plan_id"]
        if child_plan_id is None:
            continue
        child = projections.plan_state(str(child_plan_id), db=db, memo=memo)
        cause = delegation_cause(row, plan=plans_by_node[node_id], child=child)
        if cause is not None:
            causes[node_id] = [cause]
    return causes


def delegation_cause(row, *, plan, child) -> dict | None:
    """Reduce one delegated-child fact row and canonical child state to a cause."""
    from infra.queries import thread_unavailable_reason
    from plans import admission_projection, delegation_needs

    unavailable = thread_unavailable_reason(
        None
        if row["lifecycle_state"] is None
        else {
            "lifecycle_state": row["lifecycle_state"],
            "archived_reason": row["archived_reason"],
        }
    )
    if unavailable is not None:
        return {
            "kind": "blocked",
            "reason_key": DELEGATED_CHILD_UNAVAILABLE,
            "reason": f"The delegated child cannot finish this step: {unavailable}",
            "owner_kind": "thread",
            "owner_id": admission_projection.executor_of(row, plan),
            "child_thread_id": str(row["child_id"]),
            "created_at": row["attached_at"],
        }
    if cause := delegation_needs.cause(row, admission_projection.executor_of(row, plan)):
        return cause
    # A paused worker is not working, even while its Turn runs on to a safe
    # point: the step reads paused, not in progress (pause contract, #1970).
    if row["child_plan_state"] == "paused":
        return {
            "kind": "paused",
            "reason_key": DELEGATED_CHILD_PAUSED,
            "reason": "Its worker is paused",
            "owner_kind": "thread",
            "owner_id": str(row["child_id"]),
            "child_thread_id": str(row["child_id"]),
            "created_at": row["attached_at"],
        }
    review_only = (
        child.progress.liveness == "needs-review"
        and not child.moving
        and not row["active_work"]
    )
    # Queued handback or a Turn begun after review starts another round;
    # a Turn already open at review does not hide the needed intervention.
    # A stalled child counts only work actually moving below it.
    stalled = row["stalled"]
    active = bool(child.moving or (not stalled and (
        row["input_queued"]
        or (
            row["turn_running"]
            and (not review_only or row["turn_started_after_review"])
        )
    )))
    if not (active or row["open_work"] or review_only):
        return None
    if active:
        reason_key = DELEGATED_CHILD
        reason = "A delegated child is working on this step"
    elif review_only:
        reason_key = DELEGATED_CHILD_REVIEW
        reason = "The delegated child has work awaiting review"
    elif stalled:
        reason_key = DELEGATED_CHILD
        from prompts.plan_wakes import account_readiness_detail
        reason = _STALLED[stalled] + (' ' + account_readiness_detail(row.get('account_readiness')) if stalled == 'account' else '')
    else:
        reason_key = DELEGATED_CHILD
        reason = "Waiting for the delegated child to take up its open steps"
    return {
        "kind": "waiting",
        "reason_key": reason_key,
        "reason": reason,
        "owner_kind": "thread",
        "owner_id": str(row["child_id"]),
        "child_thread_id": str(row["child_id"]),
        "child_active": active,
        "created_at": row["attached_at"],
    }
