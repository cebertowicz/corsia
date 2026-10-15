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


def settled_reasons(db, nodes, *, labels: dict[str, str] | None = None) -> dict[str, str]:
    """What a settled node's row records about how it ended, by node id.

    The one reader of a settled row's recorded reason. A closed node's is the
    reason ``close_node`` wrote. With ``labels`` (the pane, which shows who did
    what), an accepted step assigned to the user that an agent marked done also
    reads ``Marked by <label>``, from the thread that recorded the accept; a
    step the user marked carries none, so the line shows only the exception.
    ``labels`` maps thread ids to display titles and is read, never extended;
    a thread it lacks is looked up once, with every other missing one.

    Takes node rows and reads only the qualifying ones, so a read with nothing
    to report costs no query. Other nodes, or ones with nothing readable, are
    absent.
    """
    closed = {str(node["id"]) for node in nodes if node["state"] == service.CLOSED}
    marked = set() if labels is None else {
        str(node["id"]) for node in nodes
        if node["state"] in service._ACCEPTED_STATES and node["assignee"] == "user"
    }
    ids = sorted(closed | marked)
    if not ids:
        return {}
    from plans.boundary import is_user_audit_id

    accepted = sorted(service._ACCEPTED_STATES)
    rows = db.execute(
        f"""SELECT node_id,new_state,payload_json FROM plan_node_events
            WHERE node_id IN ({','.join('?' for _ in ids)}) AND event='state_transition'
              AND new_state IN ({','.join('?' for _ in (service.CLOSED, *accepted))})
            ORDER BY created_at,rowid""",
        (*ids, service.CLOSED, *accepted),
    ).fetchall()
    reasons: dict[str, str] = {}
    markers: dict[str, str] = {}
    for row in rows:
        node_id = str(row["node_id"])
        payload = json.loads(row["payload_json"] or "null") or {}
        if not isinstance(payload, dict):
            continue
        if row["new_state"] == service.CLOSED:
            if node_id in closed and payload.get("reason"):
                reasons[node_id] = str(payload["reason"])
        elif node_id in marked:
            markers.pop(node_id, None)
            verifier = str(payload.get("verified_by") or "")
            if verifier and not is_user_audit_id(verifier):
                markers[node_id] = verifier
    if markers:
        from plans.system_user_steps import is_system_verifier

        known = dict(labels or {})
        known.update({who: "CORSIA" for who in markers.values() if is_system_verifier(who)})
        missing = {thread for thread in markers.values() if thread not in known}
        if missing:
            from plans.operator_node_facts import executor_labels

            known.update(executor_labels(missing, db=db))
        for node_id, thread in markers.items():
            reasons[node_id] = f"Marked by {known.get(thread, 'an agent')}"
    return reasons
