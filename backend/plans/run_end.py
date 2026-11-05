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
RUN_FAILED = "Closed because required task {title} {how}."
#: How the required step ended, by its state: it failed, or it was closed (the person's Won't do).
_HOW = {"failed": "failed"}
_NOT_DONE = "was not done"
#: Why the reply to a failed required step says the run is still open.
RUN_LEFT_OPEN = "The run stays open until these settle, since their workers still have open work: {titles}."
_PROVENANCE = "plans.workflow_run"
_DONE = "done"
#: A step the run's failure closed before anyone started it.
_NOT_RUN = "not_run"
#: A started step the run's failure cut short: never filed under work not attempted, nor under an agent's skip.
_STOPPED = "stopped"


def _run_phase(db, node) -> bool:
    """Whether ``node`` is a phase whose children are workflow steps."""
    if node["kind"] != "phase" or node["workflow_ref"] is None:
        return False
    return db.execute(
        "SELECT 1 FROM plan_nodes WHERE parent_id=? AND workflow_step IS NOT NULL LIMIT 1", (node["id"],),
    ).fetchone() is not None


def open_nodes(db, phase_id: str) -> list[str]:
    """Open work beneath a run's phase, deepest first.

    The walk is the in-work walk of ``user_settle``; a nested run's phase and a
    container whose children are in work have no work of their own: they read
    as their leaves, the same rule the fold uses.
    """
    return [str(row["id"]) for row in db.execute(
        "WITH RECURSIVE below(id,depth) AS ("
        " SELECT n.id,1 FROM plan_nodes n WHERE n.parent_id=?"
        f" AND {node_states.in_work_sql('n')}"
        " UNION ALL SELECT n.id,below.depth+1 FROM plan_nodes n JOIN below ON n.parent_id=below.id"
        f" WHERE {node_states.in_work_sql('n')})"
        " SELECT n.id FROM below JOIN plan_nodes n ON n.id=below.id"
        f" WHERE {node_states.open_sql('n')}"
        " AND NOT (n.kind='phase' AND n.workflow_step IS NOT NULL)"
        f" AND NOT {node_states.is_container_sql('n')}"
        " ORDER BY below.depth DESC,n.order_key", (phase_id,),
    )]


def fail_run(node_id: str, common: dict, how: str | None = None) -> tuple[list[str], list[str]]:
    """After a required step ends not done, close the run's other open work with the reason.

    A run that is itself a required step of an outer run fails that run too, for the same ``how``.
    Returns the ids closed and the ids whose close was refused. The closes carry
    this module's provenance, which is how the run's record tells a step never
    reached from one an agent chose to skip. Work whose close is refused (a
    delegated worker still has open work) stays open: the run ends when its
    owner settles it, and the reply says so (``RUN_LEFT_OPEN``). The caller
    releases and tells each closed node.
    """
    from plans import closure
    from plans.workflow_fields import creation_payload

    db = common["db"]
    node = service._node_row(db, node_id)
    if node["workflow_step"] is None or node["parent_id"] is None:
        return [], []
    if creation_payload(db, node_id).get("workflow_required") is not True:
        return [], []
    if not _run_phase(db, service._node_row(db, node["parent_id"])):
        return [], []
    how = how or _HOW.get(node["state"], _NOT_DONE)
    reason = RUN_FAILED.format(title=node["title"], how=how)
    closed: list[str] = []
    refused: set[str] = set()
    progress = True
    while progress:
        progress = False
        for open_id in open_nodes(db, node["parent_id"]):
            if open_id in refused or service._node_row(db, open_id)["state"] in service._TERMINAL_STATES:
                continue
            try:
                closure.close_node(
                    open_id, reason=reason, actor_id=common["actor_id"],
                    provenance=_PROVENANCE, db=db,
                )
            except service.PlanError:
                refused.add(open_id)
                continue
            closed.append(open_id)
            progress = True
    still_open = [open_id for open_id in refused if service._node_row(db, open_id)["state"] not in service._TERMINAL_STATES]
    outer_closed, outer_open = fail_run(node["parent_id"], common, how)
    return closed + outer_closed, still_open + outer_open


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
    steps, totals = _steps(db, phase)
    # Any path may close a required step (a run's failure, a retired worker, the person's Won't do):
    # a run completes only when every required step was done.
    failed = any(step["required"] and step["state"] != _DONE for step in steps)
    service._event(
        db, phase["id"], EVENT, actor_id=None, provenance=_PROVENANCE,
        payload={
            "workflow_ref": phase["workflow_ref"], "workflow_version": phase["workflow_version"],
            "size": phase["size"], "outcome": "failed" if failed else "completed", "steps": steps, **totals,
        },
    )


def _steps(db, phase) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Each step of the run in definition order, and the run's own totals, both from the cost owner.

    Cost and time are the rolled figures of ``node_cost_scoped``: they include plans
    attached to a step (delegated work) and count wall-clock time once.
    """
    from plans import closure, gate_handoff_record, node_cost_scoped
    from plans.workflow_fields import creation_payload

    by_step: dict[str, list] = {}
    for row in db.execute(
        "SELECT * FROM plan_nodes WHERE parent_id=? AND workflow_step IS NOT NULL ORDER BY created_at,rowid",
        (phase["id"],),
    ):
        by_step.setdefault(row["workflow_step"], []).append(row)
    wanted = [phase["id"], *(row["id"] for rows in by_step.values() for row in rows)]
    _own, rolled, _initiatives, _ledger = node_cost_scoped.project([], wanted, (), db=db)
    current = [rows[-1] for rows in by_step.values()]
    closed = [row for row in current if row["state"] == service.CLOSED]
    skipped = closure.settled_reasons(db, closed)
    by_failure = _closed_by_failure(db, [row["id"] for row in closed])
    listed = []
    for step, rows in by_step.items():
        last = rows[-1]
        created = creation_payload(db, rows[0]["id"])
        inner = _ended(db, last["id"]) if last["kind"] == "phase" else None
        if inner is not None:
            if all(item["state"] == _NOT_RUN for item in inner["steps"]):
                state = _NOT_RUN
            else:
                state = _DONE if inner["outcome"] == "completed" else "failed"
        else:
            state = _DONE if last["state"] in service._ACCEPTED_STATES else last["state"]
            if last["id"] in by_failure:
                state = by_failure[last["id"]]
            elif last["archived_at"] is not None and last["state"] not in service._TERMINAL_STATES:
                state = service.CLOSED  # archiving an open optional step skips it
        # A step's rounds never overlap (a send-back or follow-up settles a round before the
        # next row opens), so their sum is their union.
        figures = [rolled[row["id"]] for row in rows]
        listed.append({
            "step": step, "state": state, "required": created.get("workflow_required") is True,
            "by": created.get("workflow_by", "agent"), "rounds": len(rows) - 1,
            "node_ids": [str(row["id"]) for row in rows],
            "skip_reason": skipped.get(last["id"]) if state in (service.CLOSED, _STOPPED) else None,
            "execution_seconds": sum(item["execution"]["seconds"] for item in figures),
            "waiting_seconds": sum(item["waiting"]["seconds"] for item in figures),
            "model_cost_usd": _sum(item["model_cost_usd"]["known"] for item in figures),
            "cost_unknown_rows": sum(item["model_cost_usd"]["unknown_rows"] for item in figures),
            "inner_workflow_ref": last["workflow_ref"] if inner is not None else None,
            "inner_workflow_version": last["workflow_version"] if inner is not None else None,
            "gate_mode": created.get("workflow_gate_mode"),
            "gate_answer": gate_handoff_record.first_answer(db, [row["id"] for row in rows])[0]
            if created.get("workflow_gate") == "acceptance" else None,
        })
    own = rolled[phase["id"]]
    totals = {
        "execution_seconds": own["execution"]["seconds"], "waiting_seconds": own["waiting"]["seconds"],
        "model_cost_usd": own["model_cost_usd"]["known"], "cost_unknown_rows": own["model_cost_usd"]["unknown_rows"],
    }
    return listed, totals


def _closed_by_failure(db, node_ids: list[str]) -> dict[str, str]:
    """Of ``node_ids``, the ones a failed required step closed (``fail_run``): not run if never started, else stopped."""
    if not node_ids:
        return {}
    marks = ",".join("?" * len(node_ids))
    return {str(row["node_id"]): _NOT_RUN if row["prior_state"] == "pending" else _STOPPED for row in db.execute(
        "SELECT node_id,prior_state FROM plan_node_events WHERE event='state_transition' AND new_state=?"
        f" AND provenance=? AND node_id IN ({marks})",
        (service.CLOSED, _PROVENANCE, *node_ids),
    )}


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
    since = None
    line = None
    parent_id = node["parent_id"]
    while parent_id is not None:
        parent = service._node_row(db, parent_id)
        if parent["kind"] == "phase" and parent["workflow_ref"] is not None:
            if since is None:
                since = db.execute(
                    "SELECT MAX(rowid) AS mark FROM plan_node_events WHERE node_id=? AND event='state_transition'",
                    (node_id,),
                ).fetchone()["mark"] or 0
            row = db.execute(
                "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event=? AND rowid>? LIMIT 1",
                (parent_id, EVENT, since),
            ).fetchone()
            if row is not None:
                line = _line(json.loads(row["payload_json"]))
        parent_id = parent["parent_id"]
    return line


def _line(record: dict[str, Any]) -> str:
    steps = record["steps"]
    count = lambda state: sum(1 for step in steps if step["state"] == state)  # noqa: E731
    cost = record["model_cost_usd"]
    seconds = record["execution_seconds"]
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    elapsed = f"{hours}h {minutes}m" if hours else f"{minutes}m {secs}s" if minutes else f"{secs}s"
    stopped = f"{count(_STOPPED)} stopped, " if count(_STOPPED) else ""
    return (
        f"Run {record['workflow_ref']} {record['outcome']}: {count(_DONE)} done, "
        f"{count(service.CLOSED)} skipped, {stopped}{count(_NOT_RUN)} not run, {count('failed')} failed, {elapsed}, "
        + ("cost unknown" if cost is None or record["cost_unknown_rows"] else f"${cost:.2f}") + "."
    )
