"""How a workflow run ends: a failed required step ends it, and the last close records it.

A run is a phase built from a workflow definition; its steps are the phase's
children and never change the phase's own state (a parent reads as the roll-up
of its children). So the end of a run is a fact about its steps, written once
as a ``workflow_run_ended`` event on the phase.
"""

from __future__ import annotations

import json
from typing import Any

import plans.service as service
from plans import node_states

EVENT = "workflow_run_ended"
RUN_FAILED = "run failed: required step {step} failed"
_PROVENANCE = "plans.workflow_run"
_DONE = "done"


def _run_phase(db, node) -> bool:
    """Whether ``node`` is a phase whose children are workflow steps."""
    if node["kind"] != "phase" or node["workflow_ref"] is None:
        return False
    return db.execute(
        "SELECT 1 FROM plan_nodes WHERE parent_id=? AND workflow_step IS NOT NULL LIMIT 1", (node["id"],),
    ).fetchone() is not None


def open_nodes(db, phase_id: str) -> list[str]:
    """Open work beneath a run's phase, deepest first.

    A nested run's phase and a container whose children are in work have no work
    of their own: they read as their leaves, the same rule the fold uses.
    """
    return [str(row["id"]) for row in db.execute(
        "WITH RECURSIVE below(id,depth) AS ("
        " SELECT n.id,1 FROM plan_nodes n WHERE n.parent_id=?"
        f" AND {node_states.unarchived_sql('n')}"
        " UNION ALL SELECT n.id,below.depth+1 FROM plan_nodes n JOIN below ON n.parent_id=below.id"
        f" WHERE {node_states.unarchived_sql('n')})"
        " SELECT n.id FROM below JOIN plan_nodes n ON n.id=below.id"
        f" WHERE {node_states.open_sql('n')}"
        " AND NOT (n.kind='phase' AND n.workflow_step IS NOT NULL)"
        f" AND NOT {node_states.is_container_sql('n')}"
        " ORDER BY below.depth DESC,n.order_key", (phase_id,),
    )]


def fail_run(node_id: str, common: dict, *, executor: str | None) -> None:
    """After a required step fails, close the run's other open work with the reason."""
    from plans import boundary_after, closure
    from plans.workflow_fields import creation_payload

    db = common["db"]
    node = service._node_row(db, node_id)
    if node["workflow_step"] is None or node["parent_id"] is None:
        return
    if creation_payload(db, node_id).get("workflow_required") is not True:
        return
    if not _run_phase(db, service._node_row(db, node["parent_id"])):
        return
    reason = RUN_FAILED.format(step=node["workflow_step"])
    while open_ids := open_nodes(db, node["parent_id"]):
        for open_id in open_ids:
            if service._node_row(db, open_id)["state"] in service._TERMINAL_STATES:
                continue
            closure.close_node(
                open_id, reason=reason, actor_id=common["actor_id"],
                provenance=common["provenance"], db=db,
            )
            boundary_after.after_transition(open_id, service.CLOSED, common, executor=executor)


def record_if_ended(db, node_id: str, new_state: str) -> None:
    """After a node settles, record each run above it that has no open work left.

    A node accepted with a follow-up is not the end of its step: the follow-up is created next.
    """
    if new_state == "accepted_with_followup":
        return
    parent_id = service._node_row(db, node_id)["parent_id"]
    while parent_id is not None:
        parent = service._node_row(db, parent_id)
        if _run_phase(db, parent):
            if open_nodes(db, parent_id):
                return
            _record(db, parent)
        parent_id = parent["parent_id"]


def _ended(db, phase_id: str) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event=? ORDER BY created_at,rowid LIMIT 1",
        (phase_id, EVENT),
    ).fetchone()
    return None if row is None else json.loads(row["payload_json"])


def _record(db, phase) -> None:
    """Write the one ``workflow_run_ended`` event of ``phase``; a second call writes nothing."""
    if _ended(db, phase["id"]) is not None:
        return
    steps = _steps(db, phase)
    failed = any(step["required"] and step["state"] == "failed" for step in steps)
    service._event(
        db, phase["id"], EVENT, actor_id=None, provenance=_PROVENANCE,
        payload={
            "workflow_ref": phase["workflow_ref"], "workflow_version": phase["workflow_version"],
            "size": phase["size"], "outcome": "failed" if failed else "completed", "steps": steps,
        },
    )


def _steps(db, phase) -> list[dict[str, Any]]:
    """Each step of the run in definition order, from the node that is currently the step."""
    from plans import closure, node_cost
    from plans.workflow_fields import creation_payload

    by_step: dict[str, list] = {}
    for row in db.execute(
        "SELECT * FROM plan_nodes WHERE parent_id=? AND workflow_step IS NOT NULL ORDER BY created_at,rowid",
        (phase["id"],),
    ):
        by_step.setdefault(row["workflow_step"], []).append(row)
    own = node_cost.own_many([row["id"] for rows in by_step.values() for row in rows], db=db)
    current = [rows[-1] for rows in by_step.values()]
    skipped = closure.settled_reasons(db, [row for row in current if row["state"] == service.CLOSED])
    listed = []
    for step, rows in by_step.items():
        last = rows[-1]
        created = creation_payload(db, rows[0]["id"])
        inner = _ended(db, last["id"]) if last["kind"] == "phase" else None
        if inner is not None:
            state = _DONE if inner["outcome"] == "completed" else "failed"
            seconds = sum(item["execution_seconds"] for item in inner["steps"])
            waiting = sum(item["waiting_seconds"] for item in inner["steps"])
            cost = _sum(item["model_cost_usd"] for item in inner["steps"])
        else:
            state = _DONE if last["state"] in service._ACCEPTED_STATES else last["state"]
            seconds = sum(own[row["id"]]["execution"]["seconds"] for row in rows)
            waiting = sum(own[row["id"]]["waiting"]["seconds"] for row in rows)
            cost = _sum(own[row["id"]]["model_cost_usd"]["known"] for row in rows)
        listed.append({
            "step": step, "state": state, "required": created.get("workflow_required") is True,
            "by": created.get("workflow_by", "agent"), "rounds": len(rows) - 1,
            "skip_reason": skipped.get(last["id"]) if last["state"] == service.CLOSED else None,
            "execution_seconds": seconds, "waiting_seconds": waiting, "model_cost_usd": cost,
            "inner_workflow_ref": last["workflow_ref"] if inner is not None else None,
            "inner_workflow_version": last["workflow_version"] if inner is not None else None,
        })
    return listed


def _sum(values) -> float | None:
    from plans.node_cost import sum_known

    return sum_known(values)


def summary(db, node_id: str) -> str | None:
    """One line on the run the call that settled ``node_id`` just ended, if it ended one.

    A run counts as just ended when its record was written after the node's own
    last transition; of nested runs ending together, the outermost speaks.
    """
    node = service._node_row(db, node_id)
    if node["state"] not in service._TERMINAL_STATES:
        return None
    since = db.execute(
        "SELECT MAX(rowid) AS mark FROM plan_node_events WHERE node_id=? AND event='state_transition'",
        (node_id,),
    ).fetchone()["mark"]
    line = None
    parent_id = node["parent_id"]
    while parent_id is not None:
        parent = service._node_row(db, parent_id)
        row = db.execute(
            "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event=? AND rowid>? LIMIT 1",
            (parent_id, EVENT, since or 0),
        ).fetchone()
        if row is not None:
            line = _line(json.loads(row["payload_json"]))
        parent_id = parent["parent_id"]
    return line


def _line(record: dict[str, Any]) -> str:
    steps = record["steps"]
    count = lambda state: sum(1 for step in steps if step["state"] == state)  # noqa: E731
    cost = _sum(step["model_cost_usd"] for step in steps)
    seconds = sum(step["execution_seconds"] for step in steps)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    elapsed = f"{hours}h {minutes}m" if hours else f"{minutes}m {secs}s" if minutes else f"{secs}s"
    return (
        f"Run {record['workflow_ref']} {record['outcome']}: {count(_DONE)} done, "
        f"{count(service.CLOSED)} skipped, {count('failed')} failed, {elapsed}, "
        + ("cost unknown" if cost is None else f"${cost:.2f}") + "."
    )
