"""Retire one plan node without stranding the work that pointed at it.

`plan_nodes.archived_at` has existed since P1, and every live projection —
current, operator, rollup, progress, liveness, sibling ordering, dependency
endpoints — already filters on it. What was missing is the operation: the P2b
instruction tells agents to prune stale nodes *through supported structured
mutations*, and none existed. So this module adds the write, not the concept.

Archive is a retirement, never a delete of the node: the row, its events, its
artifact links and its history all survive, and the state is left exactly as it
was so archive can never be mistaken for acceptance. Its dependency edges are
the one exception, and the next paragraph is what happens to them.

Its dependency edges go with it. ``plans.dependencies.drop_edges_for_node``
deletes every edge touching the node in this same write, recording each removal
against the edge's dependent and settling the blocked truth that changes. This
replaces a refusal: archive used to demand the owner call ``remove_dependency``
for every edge first, which is the per-node walk this release exists to remove.
What a failed or closed prerequisite means for a live dependent is a separate
question, and ``node_waits.waits_on_dependency`` is deliberately unchanged.

It is also **not recursive**. Three things would otherwise be silently stranded
behind an invisible node, and each is refused or settled rather than detached:

* an unarchived child, or a **reachable** spawned thread whose own plan still
  has open work. Reachability, not mere existence, is load-bearing: a retired
  (archived or purged) or missing child thread can never resume to settle that
  work itself, so treating its open work as still-blocking would freeze this
  node forever with no path out. Only a thread that could still act — active
  or dormant — keeps the refusal, mirroring the same reachability the plan
  intake and decision-projection readers already use;
* a **live delegation attachment**. Detaching would drop the parent's review
  obligation over a finished-but-unharvested child and destroy the node's
  ability to disclose what it delegated, so archive refuses and names the
  settlement that owns the verdict. Terminal attachments stay on the node as
  audit history and block nothing;
* an armed watch, which would pin the owner thread in ``waiting_on_watch``
  forever, and any residual node attention.

Watch settlement goes through the watch owner's own ``resolve_watch``: it stops
the watch with no wake, where ``expire_watch`` would route a false
``delivery_failure``. Nothing here writes another lane's state.
"""

from __future__ import annotations

import plans.dependencies as dependencies
import plans.errors as errors
import plans.node_states as node_states
import plans.open_descendants as open_descendants
import plans.service as service

#: How many blocking ids a refusal lists before it stops. Enough to act on,
#: bounded so one refusal cannot carry a whole plan back to the caller.
_NAMED_BLOCKERS = 10

#: Each refusal names the operation that owns the fix, and the exact rows that
#: blocked it. Naming them is the difference between a refusal an owner can act
#: on and one that sends it hunting: these ids are all inside the caller's own
#: plan, or inside a child plan it delegated, so none of them is a disclosure.
_CHILDREN_REFUSED = (
    "archive is not recursive: archive this node's children first"
)
_OPEN_CHILDREN_REFUSED = (
    "close_node would strand open children: settle or close them first"
)
_SPAWNED_REFUSED = (
    "archive requires the spawned thread's own plan work to be settled first; "
    "close_node ends that work with this node, or wait for the child"
)
_DELEGATION_REFUSED = (
    "archive requires this node's delegation to be settled first: harvest the "
    "child's result, then accept, reject or fail this node"
)
_DELEGATION_REFUSED_UNREACHABLE = (
    "archive requires this node's delegation to be settled first: the child "
    "thread is retired, so its result can never be harvested — fail or "
    "reject this node instead, then archive"
)


def archive_node(node_id: str, *, actor_id: str | None = None,
                 provenance: str = "plan_service",
                 expected_revision: int | None = None, db=None) -> service.PlanNode:
    """Archive one node after proving nothing live still depends on it."""
    with service._connection(db) as connection, service._write(connection):
        node = service._node_row(connection, node_id)
        # Archived only, not ``require_live_node``: a closed node may still be
        # archived off its plan (#1887 R6).
        if node["archived_at"] is not None:
            raise service.PlanError(service.ARCHIVED_NODE)
        if expected_revision is not None:
            errors.require_node_revision(node, expected_revision)
        _require_no_children(connection, node_id)
        # Order is load-bearing: when the child is still working, that is the
        # actionable message. The delegation refusal is reserved for the case
        # where the child is genuinely finished and only the verdict is missing.
        _require_settled_spawned_work(connection, node)
        _require_settled_delegation(connection, node)
        # After every refusal, so a refused archive leaves the graph untouched.
        from plans import ask_attention

        ask_attention.withdraw_node_asks(
            connection, node_id, actor_id=actor_id, provenance=provenance,
        )
        dependencies.drop_edges_for_node(
            connection, node_id, actor_id=actor_id, provenance=provenance,
            plan_id=node["plan_id"],
        )
        now = service._now()
        service.touch_node(connection, node_id, at=now, archived_at=now)
        event_id = service._event(
            connection, node_id, "node_archived", actor_id=actor_id,
            provenance=provenance, payload={"state": node["state"]},
        )
        from plans.cross_plan import conductor_task_settled
        from registry.node_retirement import retire_registrations_for_node

        # A task that already settled told its conductor then; archiving it
        # afterwards is cleanup, not a second outcome.
        if node["state"] not in service._TERMINAL_STATES:
            conductor_task_settled(connection, node_id, outcome="archived")

        retire_registrations_for_node(
            connection,
            node_id=node_id,
            trigger="archived",
            causal_event_id=event_id,
        )
        settle_watches(connection, node_id)
        return service._node_from_row(
            connection, service._node_row(connection, node_id),
        )


def release_closed(connection, node, *, actor_id: str | None,
                   provenance: str) -> None:
    """End what a closed node still holds, leaving it on its plan (#1887).

    A close is refused on the same grounds as an archive -- open children, a
    reachable child still working, a live delegation -- except that a child
    already settled no longer blocks: closed and finished children stay on the
    plan now, so only an open one would be stranded. Past the guards the node
    loses its dependency edges and its armed watches, exactly as an archive
    takes them, but keeps its place: the finished-work fold is what takes it
    out of view.
    """
    _require_no_open_children(connection, node["id"])
    _require_settled_spawned_work(connection, node)
    _require_settled_delegation(connection, node)
    release_held(connection, node, actor_id=actor_id, provenance=provenance)


def release_held(connection, node, *, actor_id: str | None,
                 provenance: str) -> None:
    """Drop a closed node's dependency edges and settle its armed watches.

    The one release every close shares, past whatever guards its caller runs:
    ``release_closed`` for a runtime close, a send-back (``plans.rejection``),
    and schemas 49 -> 50 and 56 -> 57 for the nodes they close, so a migrated
    close holds exactly what a runtime one does.
    """
    dependencies.drop_edges_for_node(
        connection, node["id"], actor_id=actor_id, provenance=provenance,
        plan_id=node["plan_id"],
    )
    settle_watches(connection, node["id"])


def archive_nodes(node_ids, *, actor_id: str | None = None,
                  provenance: str = "plan_service", db=None) -> dict:
    """Archive many nodes in one write, reporting each outcome separately.

    One refusal must not discard the rest of the work: a plan cleanup names the
    nodes an owner believes are done, and a single live child somewhere in that
    list is a fact about one node, not a reason to keep the other forty. So each
    id is archived or refused on its own and both lists come back.

    The ids are applied in the caller's order, which is what makes the obvious
    retry work: naming a child before its parent cleans the whole branch in one
    call, because the child is already archived when the parent's guard runs.

    Returns:
        ``{"archived": [{"node_id": ...}], "refused": [{"node_id", "reason"}]}``.
    """
    archived: list[dict] = []
    refused: list[dict] = []
    with service._connection(db) as connection, service._write(connection):
        for node_id in node_ids:
            try:
                # Each node gets the identical guards and audit trail as the
                # single-node call; nothing is relaxed for being in a list.
                archive_node(
                    node_id, actor_id=actor_id, provenance=provenance,
                    db=connection,
                )
            except (service.PlanError, service.PlanInputError) as refusal:
                refused.append({"node_id": node_id, "reason": str(refusal)})
            else:
                archived.append({"node_id": node_id})
    return {"archived": archived, "refused": refused}


def _naming(refusal: str, ids) -> str:
    """Append the exact blocking ids to a refusal, bounded and ordered.

    Callers fetch one id past the bound, which proves more exist without
    counting them, so the suffix says "and more" rather than inventing a total.
    """
    ids = list(ids)
    suffix = ", ".join(ids[:_NAMED_BLOCKERS])
    if len(ids) > _NAMED_BLOCKERS:
        suffix += " and more"
    return f"{refusal}: {suffix}"


def _require_no_children(connection, node_id: str) -> None:
    """Keep archive one node deep so no child is retired without its own audit."""
    children = [
        str(row["id"])
        for row in connection.execute(
            "SELECT id FROM plan_nodes WHERE parent_id=? AND archived_at IS NULL "
            "ORDER BY order_key,created_at LIMIT ?",
            (node_id, _NAMED_BLOCKERS + 1),
        )
    ]
    if children:
        raise service.PlanError(_naming(_CHILDREN_REFUSED, children))


def _require_no_open_children(
    connection, node_id: str, refusal: str = _OPEN_CHILDREN_REFUSED,
) -> None:
    """Refuse to close a node over a descendant that is still open work.

    The count stored on the node says whether anything beneath it is open, so a
    close over settled history reads one row. The walk descends only through
    nodes in the work: beneath a retired one nothing is open, which is the
    invariant this guard and ``service.require_live_node`` together keep.
    """
    counts = open_descendants.counted(connection, node_id)
    if counts is not None:
        children = open_descendants.named(
            connection, node_id, _NAMED_BLOCKERS + 1, open_below=bool(counts[0]),
        )
    else:
        children = _walk_open_children(connection, node_id)
    if children:
        raise service.PlanError(_naming(refusal, children))


def _walk_open_children(connection, node_id: str) -> list[str]:
    """The open descendants, by walking the subtree; for a database without stored counts."""
    terminal = tuple(sorted(service._TERMINAL_STATES))
    return [
        str(row["id"])
        for row in connection.execute(
            "WITH RECURSIVE below(id,state,order_key,created_at) AS ("
            " SELECT n.id,n.state,n.order_key,n.created_at FROM plan_nodes n"
            f" WHERE n.parent_id=? AND {node_states.in_work_sql('n')}"
            " UNION ALL SELECT n.id,n.state,n.order_key,n.created_at"
            " FROM plan_nodes n JOIN below ON n.parent_id=below.id"
            f" WHERE {node_states.in_work_sql('n')})"
            f" SELECT id FROM below WHERE state NOT IN ({','.join('?' * len(terminal))})"
            " ORDER BY order_key,created_at LIMIT ?",
            (node_id, *terminal, _NAMED_BLOCKERS + 1),
        )
    ]


def _require_settled_spawned_work(connection, node) -> None:
    """Refuse a node whose attached, still-reachable child has open work."""
    from plans import projections

    spawned = node["spawned_thread_id"]
    if spawned is None or not _child_thread_reachable(connection, spawned):
        return
    child = connection.execute(
        "SELECT id FROM plans WHERE owner_kind='thread' AND owner_id=? "
        "AND state='active'",
        (spawned,),
    ).fetchone()
    if child is None:
        return
    open_ids = projections.open_delegated_node_ids(
        connection, child["id"], limit=_NAMED_BLOCKERS + 1,
    )
    if open_ids:
        raise service.PlanError(_naming(_SPAWNED_REFUSED, open_ids))


def _child_thread_reachable(connection, thread_id: str) -> bool:
    """Return whether a spawned thread can still settle its own plan work.

    Active and dormant threads are reachable — dormant is merely idle and
    resumes on fresh activity. A missing, archived, or purged thread is not:
    the same predicate ``plans.intake`` and ``plans.decision_projection``
    already use to decide whether a thread can still be routed to.
    """
    row = connection.execute(
        "SELECT archived_at, purged_at, lifecycle_state FROM threads WHERE id=?",
        (thread_id,),
    ).fetchone()
    return (
        row is not None
        and row["archived_at"] is None
        and row["purged_at"] is None
        and (row["lifecycle_state"] or "active") != "archived"
    )


def _require_settled_delegation(connection, node) -> None:
    """Refuse to end a delegation the node's own state still owns.

    The message must not lead with harvest when the child is unreachable:
    harvest can never complete once the child thread is retired, so naming it
    first would recreate the exact dead end this module exists to remove. The
    node still owes its own verdict either way — only the actionable path
    differs, so ``fail_node``/``reject_review`` (unconditional transitions)
    are named instead when the child cannot be harvested from.
    """
    from plans import delegation

    spawned = node["spawned_thread_id"]
    if spawned is None:
        return
    if delegation._is_live_attachment(node):
        if _child_thread_reachable(connection, spawned):
            raise service.PlanError(_DELEGATION_REFUSED)
        raise service.PlanError(_DELEGATION_REFUSED_UNREACHABLE)


def settle_watches(connection, node_id: str) -> None:
    """Resolve watches a node that stopped waiting still holds.

    Callers: this archive, and Mark done, whose user settled the wait.
    """
    from automation import plan_liveness, watches

    armed = connection.execute(
        "SELECT id, owner_thread_id FROM expected_event_watches "
        "WHERE plan_node_id=? AND status='armed' ORDER BY id",
        (node_id,),
    ).fetchall()
    for row in armed:
        watches.resolve_watch(row["id"], db=connection)
    # Arming a watch cancels the owner's plan wake, so re-derive liveness for
    # every thread whose watch just went away — otherwise the thread is left
    # with neither a watch nor a wake.
    for thread_id in sorted(
        {row["owner_thread_id"] for row in armed if row["owner_thread_id"]}
    ):
        plan_liveness.on_turn_complete(thread_id, db=connection)
