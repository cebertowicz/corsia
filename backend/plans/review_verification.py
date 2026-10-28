"""Canonical acceptance-verifier rules for structured plan nodes."""

from __future__ import annotations

import sqlite3

import plans.service as service
from plans import node_states, open_descendants

_ACCEPTED_SQL = ",".join(f"'{state}'" for state in sorted(service._ACCEPTED_STATES))


def _require_current_evidence(connection, node) -> None:
    from plans import review_evidence

    evidence = review_evidence.latest(
        node["id"],
        requirements_revision=node["requirements_revision"],
        db=connection,
    )
    if evidence is None:
        raise service.PlanError(
            "acceptance requires review evidence for the current requirements"
        )
    for artifact_id in evidence["artifact_ids"]:
        link = connection.execute(
            """SELECT 1 FROM plan_node_artifact_links l
               JOIN artifact_identities a ON a.id=l.artifact_id
               WHERE l.node_id=? AND l.artifact_id=? AND l.role='result'
                 AND a.state='active'""",
            (node["id"], artifact_id),
        ).fetchone()
        if link is None:
            raise service.PlanError(
                "acceptance review evidence references an unavailable result artifact"
            )


def _require_children_settled(connection, node) -> None:
    """Refuse a verdict over unsettled child work.

    Nothing beneath the node may be open or failed, as the node's stored counts
    say (``plans.open_descendants``), so a verdict over settled history reads
    one row. The walk stops at a node out of the work, so a closed child's whole
    subtree -- a failed task under it included -- is out, as ``work_ids`` rules
    for the rest of the plan.
    """
    counts = open_descendants.counted(connection, node["id"])
    if counts is None:
        unsettled = _walk_unsettled(connection, node)
    else:
        open_below, failed_below = counts
        unsettled = bool(failed_below) or bool(
            open_descendants.named(connection, node["id"], 1, open_below=bool(open_below))
        )
    if unsettled:
        raise service.PlanError("acceptance requires all child work to be settled")


def _walk_unsettled(connection, node) -> bool:
    """Whether a descendant in the work is not accepted, by walking the subtree."""
    descendant = connection.execute(
        f"""WITH RECURSIVE descendants(id,state) AS (
               SELECT n.id,n.state FROM plan_nodes n
               WHERE n.parent_id=? AND {node_states.in_work_sql('n')}
               UNION ALL
               SELECT n.id,n.state
               FROM plan_nodes n JOIN descendants d ON n.parent_id=d.id
               WHERE {node_states.in_work_sql('n')}
           )
           SELECT 1 FROM descendants
           WHERE state NOT IN ({_ACCEPTED_SQL})
           LIMIT 1""",
        (node["id"],),
    ).fetchone()
    return descendant is not None


def _settle_delegated_work(
    connection, node, *, actor_id: str | None, provenance: str, verified_by: str,
) -> None:
    """Accept the steps the worker returned as part of this verdict; refuse the rest.

    The parent's acceptance of its delegated step is its verdict on what the
    worker returned -- the steps awaiting that verdict are accepted here, in
    the same write and with the same verification, and the worker is asked for
    nothing. Work the worker has not returned holds the verdict back only while
    the worker is still working on it; a worker that is idle or unreachable
    cannot settle it, so the verdict stands it down and those steps close
    (#1866).
    """
    child_thread_id = node["spawned_thread_id"]
    if child_thread_id is None:
        return
    child_plan = connection.execute(
        """SELECT id FROM plans
           WHERE owner_kind='thread' AND owner_id=? AND parent_plan_id=?""",
        (child_thread_id, node["plan_id"]),
    ).fetchone()
    if child_plan is None:
        return
    from plans import delegation_harvest, delegation_needs, projections, worker_retirement

    # A worker blocked on its parent is idle by design: its need, not its
    # Turn, says the work is unfinished (N3), so no verdict stands it down.
    need = connection.execute(
        f"SELECT {delegation_needs.open_reason_sql('?')} AS reason", (child_plan["id"],),
    ).fetchone()["reason"]
    if need is not None:
        raise service.PlanError(
            f"acceptance waits for the worker's input: it blocked on you: {need}. "
            f"Answer with delegate_send(thread_id={child_thread_id}, "
            f"plan_node_id={node['id']}), or close this step to stop the worker"
        )
    for leaf in delegation_harvest.returned_steps(connection, node):
        service._transition(
            connection, leaf["id"], "accepted", actor_id=actor_id,
            provenance=provenance, verification_result="passed",
            verified_by=verified_by,
        )
    if not projections.open_delegated_work(connection, child_plan["id"]):
        return
    if _worker_is_working(connection, node):
        raise service.PlanError(
            "acceptance requires all delegated child work to be settled: its "
            "worker is still working on it. Wait for its return, or close this "
            "step to stop the worker"
        )

    worker_retirement.close_open_steps(
        connection, child_plan["id"], actor_id, reason=worker_retirement.STOOD_DOWN,
    )


def _worker_is_working(connection, node) -> bool:
    """The step's own wait decides, so accept and the plan display never disagree."""
    from plans import wait_causes

    plan = connection.execute(
        "SELECT * FROM plans WHERE id=?", (node["plan_id"],),
    ).fetchone()
    causes = wait_causes.delegation_causes([(node, plan)], db=connection)
    return any(cause.get("child_active") for cause in causes.get(str(node["id"]), ()))


def _require_dependencies_settled(connection, node) -> None:
    # The node's own edges only, like its own holds and watches below. An
    # edge an open ancestor hands down (``node_waits``) orders when its steps
    # start; it stays the ancestor's to settle, so the person's Mark done on
    # one step still accepts it while its siblings keep waiting.
    unmet = connection.execute(
        """SELECT 1 FROM plan_node_dependencies d
           JOIN plan_nodes required ON required.id=d.depends_on_node_id
           WHERE d.node_id=?
             AND required.state NOT IN ('accepted','accepted_with_followup')
           LIMIT 1""",
        (node["id"],),
    ).fetchone()
    if unmet is not None:
        raise service.PlanError("acceptance requires all required work to be settled")


def _require_no_active_causes(connection, node) -> None:
    hold = connection.execute(
        """SELECT kind FROM plan_node_holds
           WHERE node_id=? AND cleared_at IS NULL
           ORDER BY CASE kind WHEN 'blocked' THEN 0 ELSE 1 END LIMIT 1""",
        (node["id"],),
    ).fetchone()
    if hold is not None and hold["kind"] == "blocked":
        raise service.PlanError("acceptance has unresolved blocking findings")
    if hold is not None:
        raise service.PlanError("acceptance has an unresolved pause hold")
    watch = connection.execute(
        """SELECT 1 FROM expected_event_watches
           WHERE plan_node_id=? AND status='armed' LIMIT 1""",
        (node["id"],),
    ).fetchone()
    if watch is not None:
        raise service.PlanError("acceptance has an unresolved active watch")


def require_acceptance_verification(
    connection: sqlite3.Connection,
    node,
    *,
    verification_result: str | None,
    verified_by: str | None,
    actor_id: str | None = None,
    provenance: str = "plan_service",
) -> None:
    """Require current evidence and every structural obligation to be settled.

    One obligation this verdict settles itself: the steps a worker returned
    for it. ``actor_id`` and ``provenance`` are what those acceptances record.
    """

    if verification_result != "passed" or not service._required_text(
        verified_by or "", "verified by"
    ):
        raise service.PlanError("acceptance requires a passed verification result")
    _require_current_evidence(connection, node)
    _require_children_settled(connection, node)
    _settle_delegated_work(
        connection, node, actor_id=actor_id, provenance=provenance,
        verified_by=str(verified_by),
    )
    _require_dependencies_settled(connection, node)
    _require_no_active_causes(connection, node)
