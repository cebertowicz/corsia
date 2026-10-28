"""Dependency edges and the blocked delta each one settles.

Split out of ``plans.service`` for the same reason ``plans.mutations`` and
``plans.followups`` are: they are writers over one concern, and `service` is the
transaction/invariant core they all share. `service` re-exports both entry
points, so every existing caller keeps working.

`plan_node_dependencies`, its triggers, `node_waits.waits_on_dependency` and
`plans.material_events` all predate this module — until it existed, nothing in
production could write an edge, so PRD A3/A4 `depends_on[]` and derived
`blocked` were unreachable and PLANS-AC11/AC19 rendered an always-empty set.
These are *positioning* operations: they change where a node sits in the graph
and therefore its derived `blocked`, never its own state.
"""

from __future__ import annotations

import plans.admission as admission
import plans.errors as errors
import plans.material_events as material_events
import plans.node_states as node_states
from plans.service import (
    _TERMINAL_STATES,
    PlanError,
    PlanInputError,
    _connection,
    _event,
    _now,
    _write,
)


#: One answer for every unusable endpoint. `_node_row` names the id it could not
#: find, which would separate "no such node" from "exists in a plan you may not
#: read" — the caller supplies both ids, so those two must be indistinguishable
#: here and at the boundary's read check (``boundary._authorize_prerequisite``).
_DEPENDENCY_REFUSED = "dependencies must join distinct live nodes in plans you can read"


def _dependency_endpoints(connection, node_id: str, depends_on_node_id: str):
    """Resolve both ends of an edge, or refuse without disclosing either id.

    The two refusals share one message and differ only in class. A caller
    already knows whether it sent the same id twice, so naming that case
    caller-fixable discloses nothing the lookup refusal is hiding: the message
    stays single-identity, and only the never-fixable-by-re-reading case is
    reported as one.
    """
    if node_id == depends_on_node_id:
        raise PlanInputError(_DEPENDENCY_REFUSED)
    rows = {
        row["id"]: row
        for row in connection.execute(
            "SELECT * FROM plan_nodes WHERE id IN (?,?)",
            (node_id, depends_on_node_id),
        ).fetchall()
    }
    node, prerequisite = rows.get(node_id), rows.get(depends_on_node_id)
    if (
        node is None
        or prerequisite is None
        or node_states.retired(node)
        or node_states.retired(prerequisite)
    ):
        raise PlanError(_DEPENDENCY_REFUSED)
    return node, prerequisite


def add_dependency(node_id: str, depends_on_node_id: str, *, actor_id: str | None = None,
                   provenance: str = "plan_service", expected_revision: int | None = None,
                   causing_executor: str | None = None, db=None) -> None:
    """Add a dependency edge only when it keeps the graph acyclic.

    The prerequisite may sit in another plan (#1552); the boundary has already
    checked the caller may read it. The edge lives on the dependent, so the
    other plan is not written: ``plans.cross_plan`` tells this plan's owner
    when that prerequisite settles.

    ``causing_executor`` is the thread whose own write this is, when one is
    acting: the edge changes eligibility and reselects work, and the thread
    that changed it already knows.
    """
    with _connection(db) as connection, _write(connection):
        node, prerequisite = _dependency_endpoints(
            connection, node_id, depends_on_node_id,
        )
        if expected_revision is not None:
            errors.require_node_revision(node, expected_revision)
        cycle = connection.execute(
            "WITH RECURSIVE prerequisites(id) AS ("
            " SELECT depends_on_node_id FROM plan_node_dependencies WHERE node_id=?"
            " UNION SELECT d.depends_on_node_id FROM plan_node_dependencies d "
            "JOIN prerequisites p ON d.node_id=p.id) "
            "SELECT 1 FROM prerequisites WHERE id=? LIMIT 1",
            (depends_on_node_id, node_id),
        ).fetchone()
        if cycle is not None:
            raise PlanError("dependency would create a cycle")
        before = material_events.blocked_snapshot(connection, (node_id,))
        inserted = connection.execute(
            "INSERT OR IGNORE INTO plan_node_dependencies "
            "(node_id,depends_on_node_id,created_at) VALUES (?,?,?)",
            (node_id, depends_on_node_id, _now()),
        )
        if inserted.rowcount:
            event_id = _event(
                connection, node_id, "dependency_added", actor_id=actor_id,
                provenance=provenance,
                payload={"depends_on_node_id": depends_on_node_id},
            )
            material_events.settle_blocked_deltas(
                connection, before, actor_id=actor_id, provenance=provenance,
                causal_event_id=event_id,
            )
            if admission.project(node_id, db=connection)["condition"] == "blocked":
                admission.release(node_id, "dependency_blocked", db=connection)
            admission.reconcile_plan(
                node["plan_id"], actor_id=actor_id, provenance=provenance,
                causing_executor=causing_executor, db=connection,
            )


def remove_dependency(node_id: str, depends_on_node_id: str, *,
                      actor_id: str | None = None,
                      provenance: str = "plan_service",
                      expected_revision: int | None = None,
                      causing_executor: str | None = None, db=None) -> bool:
    """Remove one dependency edge and settle the resulting blocked delta.

    Symmetric with :func:`add_dependency`, including the snapshot: the delta is
    computed over the *dependent* node, so a node that still has another unmet
    prerequisite stays blocked and no false `blocked_cleared` fires.

    Removing an edge that is not there is a no-op — no event, no delta, no
    attention change — so a retry after a lost response cannot invent history.

    Returns:
        Whether an edge was actually removed. A retry after a lost response
        gets ``False`` and can tell that from the first call's ``True``,
        instead of reading an unchanged graph and guessing which call made it.
    """
    with _connection(db) as connection, _write(connection):
        node, _prerequisite = _dependency_endpoints(
            connection, node_id, depends_on_node_id,
        )
        if expected_revision is not None:
            errors.require_node_revision(node, expected_revision)
        from plans import node_waits

        direct = connection.execute(
            "SELECT 1 FROM plan_node_dependencies WHERE node_id=? AND depends_on_node_id=?",
            (node_id, depends_on_node_id),
        ).fetchone()
        if direct is None:
            rows = node_waits.unmet_prerequisites(connection, [node_id], include_met=True).get(node_id, [])
            inherited = next((row for row in rows if row["id"] == depends_on_node_id and row["via"]), None)
            if inherited is not None:
                raise PlanInputError(f"Dependency is inherited from ancestor {inherited['via']}; remove it there")
        before = material_events.blocked_snapshot(connection, (node_id,))
        deleted = connection.execute(
            "DELETE FROM plan_node_dependencies "
            "WHERE node_id=? AND depends_on_node_id=?",
            (node_id, depends_on_node_id),
        )
        if deleted.rowcount:
            event_id = _event(
                connection, node_id, "dependency_removed", actor_id=actor_id,
                provenance=provenance,
                payload={"depends_on_node_id": depends_on_node_id},
            )
            material_events.settle_blocked_deltas(
                connection, before, actor_id=actor_id, provenance=provenance,
                causal_event_id=event_id,
            )
            admission.reconcile_plan(
                node["plan_id"], actor_id=actor_id, provenance=provenance,
                causing_executor=causing_executor, db=connection,
            )
        return bool(deleted.rowcount)


def move_dependents(connection, from_node_id: str, to_node_id: str, *,
                    actor_id: str | None, provenance: str) -> None:
    """Point every edge on a rejected submission at its revision, in the caller's write.

    A rejected submission is terminal and its work continues on the revision
    (``plans.rejection``), so a dependent left waiting on the submission would
    read ``dependency_failed`` forever: accepting the revision could never
    satisfy an edge that names another node (#1730). Only open, unarchived
    dependents move: settled work is never rewritten. The chain of revisions
    only ever grows forward, so the move cannot close a cycle. Each move is
    recorded on the dependent as the removal and addition it is, the same
    events :func:`remove_dependency` and :func:`add_dependency` write, so an
    edge's history reads identically however it changed; a dependent that
    already waits on the revision loses only the stale edge.
    """
    rows = connection.execute(
        f"""SELECT d.node_id, EXISTS (
                 SELECT 1 FROM plan_node_dependencies t
                  WHERE t.node_id=d.node_id AND t.depends_on_node_id=?
               ) AS has_target
              FROM plan_node_dependencies d JOIN plan_nodes n ON n.id=d.node_id
             WHERE d.depends_on_node_id=? AND n.archived_at IS NULL
               AND n.state NOT IN ({",".join("?" for _ in _TERMINAL_STATES)})
             ORDER BY d.created_at, d.node_id""",
        (to_node_id, from_node_id, *sorted(_TERMINAL_STATES)),
    ).fetchall()
    if not rows:
        return
    dependents = [str(row["node_id"]) for row in rows]
    before = material_events.blocked_snapshot(connection, dependents)
    now = _now()
    for row, dependent in zip(rows, dependents):
        connection.execute(
            "DELETE FROM plan_node_dependencies WHERE node_id=? AND depends_on_node_id=?",
            (dependent, from_node_id),
        )
        causal = _event(
            connection, dependent, "dependency_removed", actor_id=actor_id,
            provenance=provenance, payload={"depends_on_node_id": from_node_id},
        )
        if not row["has_target"]:
            connection.execute(
                "INSERT INTO plan_node_dependencies (node_id,depends_on_node_id,created_at) "
                "VALUES (?,?,?)",
                (dependent, to_node_id, now),
            )
            causal = _event(
                connection, dependent, "dependency_added", actor_id=actor_id,
                provenance=provenance, payload={"depends_on_node_id": to_node_id},
            )
        material_events.settle_blocked_deltas(
            connection, {dependent: before[dependent]}, actor_id=actor_id,
            provenance=provenance, causal_event_id=causal,
        )


def drop_edges_for_node(connection, node_id: str, *, actor_id: str | None,
                        provenance: str, plan_id: str,
                        causing_executor: str | None = None) -> None:
    """Drop every edge touching a node being retired, in the caller's write.

    Retirement used to be *refused* while any edge touched the node, because
    ``node_waits.waits_on_dependency`` does not filter archived prerequisites and a silent
    detach would block the dependent forever behind a node no projection shows.
    Dropping the edges removes that stranding risk outright, which is what lets
    the refusal go: the owner no longer has to walk the graph by hand before it
    can retire anything.

    Each removal is recorded against the edge's *dependent* -- the same side
    :func:`remove_dependency` records it on -- so an edge's history reads
    identically however it went away. Only dependents can change blocked truth,
    so only they are snapshotted, and each one's delta cites its own removal.
    """
    edges = [
        (str(row["node_id"]), str(row["depends_on_node_id"]))
        for row in connection.execute(
            "SELECT node_id,depends_on_node_id FROM plan_node_dependencies "
            "WHERE node_id=? OR depends_on_node_id=? "
            "ORDER BY created_at,node_id,depends_on_node_id",
            (node_id, node_id),
        ).fetchall()
    ]
    if not edges:
        return
    from plans import cross_plan

    # Read before the edges go: an unmet prerequisite leaving its plan releases
    # a dependent whose work never landed, and another plan's owner must hear it.
    foreign = cross_plan.dependents_of(connection, node_id)
    # A close someone else made also tells this plan's owner, once, below.
    own = cross_plan.own_dependents_of(connection, node_id, actor_id=actor_id)
    dependents = [dependent for dependent, _ in edges if dependent != node_id]
    before = material_events.blocked_snapshot(connection, dependents)
    connection.execute(
        "DELETE FROM plan_node_dependencies "
        "WHERE node_id=? OR depends_on_node_id=?",
        (node_id, node_id),
    )
    removals = {}
    for dependent, prerequisite in edges:
        removals[dependent] = _event(
            connection, dependent, "dependency_removed", actor_id=actor_id,
            provenance=provenance,
            payload={"depends_on_node_id": prerequisite},
        )
    for dependent, prior in before.items():
        material_events.settle_blocked_deltas(
            connection, {dependent: prior}, actor_id=actor_id,
            provenance=provenance, causal_event_id=removals[dependent],
        )
    # The owner told of a close hears what is now current in that one wake,
    # so it counts as the causing executor here and gets one message, not two.
    admission.reconcile_plan(
        plan_id, actor_id=actor_id, provenance=provenance,
        causing_executor=causing_executor or cross_plan.told_owner(own), db=connection,
    )
    cross_plan.prerequisite_removed(
        connection, node_id, foreign, actor_id=actor_id, provenance=provenance,
        own_dependents=own,
    )
