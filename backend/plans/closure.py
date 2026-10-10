"""End one plan node as no longer wanted, and keep it on the plan as that record.

Until this existed a plan had three outcomes and none of them said "we are not
doing this": ``accepted`` met the objective, ``failed`` says it *could not* be
met, and ``archive_node`` deliberately takes no position on the work at all so
a retirement can never read as acceptance. Agents therefore marked superseded
work failed or blocked, or left it open, and either way the plan stopped
telling the truth about what it was still committed to.

``closed`` is that outcome. A closed node stays on its plan like any finished
node (#1887, owner 2026-09-24): it sinks into its level's finished fold with
its close reason, so the person accountable for it learns it was dropped from
the plan they already read. It is out of the work, exactly as an archived
node always was (``node_states.work_ids``): it counts in neither ``done`` nor
``total``, liveness never offers it, the upkeep notice never names it, and it
is nobody's child, so closing a parent's last open child hands the parent back
as open work. Archive stays the separate, explicit act that takes a node off
the plan. Nodes closed before this (archived in the same write) came back onto
their plans in the schema 42 -> 43 migration.

Rule 1b (owner, 2026-09-16): the owning agent can always clean its own plan. No
guard here reads ``assignee``. A user assignment governs who does the work and
who may take the executor slot; it is not lifecycle immunity, and the node it
stranded in issue #1827 is the reason this module refuses to add one.

Closing does not make an open child or an unsettled delegation disappear: a
close that would strand one is refused by ``plans.archival.release_closed``,
naming the exact rows that block it.
"""

from __future__ import annotations

import json

import plans.errors as errors
import plans.service as service

#: The one refusal for a close that records nothing. Closing is the only
#: outcome whose entire content is why, so an empty reason is an empty outcome.
REASON_REQUIRED = (
    "close_node requires a reason: say why this work will not be done"
)


def close_node(
    node_id: str, *, reason: str, actor_id: str | None = None,
    provenance: str = "plan_service", expected_revision: int | None = None,
    db=None,
) -> service.PlanNode:
    """Close one node as no longer wanted; it stays on the plan, folded.

    Raises:
        PlanInputError: ``reason`` is empty.
        PlanError: The node's state has no legal move to ``closed``, or a
            guard refuses -- both roll the close back whole.
    """
    from plans import archival

    recorded = (reason or "").strip()
    if not recorded:
        raise service.PlanInputError(REASON_REQUIRED)
    with service._connection(db) as connection, service._write(connection):
        node = service._live_node_row(connection, node_id)
        if expected_revision is not None:
            errors.require_node_revision(node, expected_revision)
        service._transition(
            connection, node_id, service.CLOSED, actor_id=actor_id,
            provenance=provenance, verification_result=None, verified_by=None,
            payload={"reason": recorded},
        )
        # The guards run second so they judge a node that is already terminal:
        # a delegation the close itself settled no longer blocks, and anything
        # that still does refuses the whole close.
        archival.release_closed(
            connection, service._node_row(connection, node_id),
            actor_id=actor_id, provenance=provenance,
        )
        return service._node_from_row(
            connection, service._node_row(connection, node_id),
        )


def close_reasons(db, nodes) -> dict[str, str]:
    """Why each closed node closed: the reason its close recorded, by node id.

    The one reader of the reason ``close_node`` writes, for the row that shows
    it and the return that reports it. Takes node rows and reads only the
    closed ones, so a read with nothing closed costs no query. Nodes that are
    not closed, or closed without a readable reason, are absent.
    """
    ids = sorted({str(node["id"]) for node in nodes if node["state"] == service.CLOSED})
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = db.execute(
        f"""SELECT node_id,payload_json FROM plan_node_events
            WHERE node_id IN ({marks}) AND event='state_transition' AND new_state=?
            ORDER BY created_at,rowid""",
        (*ids, service.CLOSED),
    ).fetchall()
    reasons: dict[str, str] = {}
    for row in rows:
        reason = json.loads(row["payload_json"] or "null") or {}
        if isinstance(reason, dict) and reason.get("reason"):
            reasons[str(row["node_id"])] = str(reason["reason"])
    return reasons
