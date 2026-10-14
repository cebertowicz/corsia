"""System code opens and settles a step for the person in an agent's plan.

The route for a step no agent chose to ask for: the system saw the person
must act (a password page in the shared browser) and later saw it done. The
step is the user's (``assignee='user'``), attributed to the system
(``actor_id`` None plus the caller's provenance), never to the agent whose
plan holds it, so it never reads as the agent asking.

Opening is idempotent on the caller's key within that thread's plan. The
agent's selected task, if any, gains a dependency on the step, so the plan
carries the wait: settling or closing the step wakes that task through the
dependency path, and a paused plan stays paused until resume.

Settling is the person's Mark done, observed by the system: it ends the
step's stops and never refuses on a pause or hold, because those stop agents,
not the person. A step already settled, closed or archived is left alone. The
row reads ``Marked by CORSIA`` (``plans.closure.settled_reasons``).

Both join the caller's write transaction when there is one.
"""

from __future__ import annotations

import plans.boundary_after as after
import plans.errors as errors
import plans.service as service
from plans.plan_write import transaction

#: The plan event that records a system-opened step and its idempotency key.
OPENED = "system_user_step_opened"
#: The verifier prefix a system settlement records; the pane names CORSIA.
VERIFIER_PREFIX = "system:"


def open_user_step(
    db, thread_id: str, *, key: str, title: str, description: str, provenance: str,
) -> str:
    """Open the person's step in ``thread_id``'s plan once per ``key``; return its id."""
    from plans import admission_attempts, dependencies

    key = str(key).strip()
    if not key:
        raise errors.PlanInputError("open_user_step needs a non-empty key")
    with transaction(db) as connection:
        plan = service.ensure_plan("thread", thread_id, db=connection)
        existing = connection.execute(
            """SELECT e.node_id FROM plan_node_events e JOIN plan_nodes n ON n.id=e.node_id
               WHERE n.plan_id=? AND e.event=? AND json_extract(e.payload_json,'$.key')=?
               ORDER BY e.created_at,e.rowid LIMIT 1""",
            (plan.id, OPENED, key),
        ).fetchone()
        if existing is not None:
            return str(existing["node_id"])
        selected = [
            str(row["node_id"])
            for row in admission_attempts.current_for_executor(connection, thread_id)
        ]
        node = service.add_node(
            plan.id, kind="task", title=title, acceptance_criteria=title,
            description=description, assignee="user", actor_id=None,
            provenance=provenance, db=connection,
        )
        service._event(connection, node.id, OPENED, actor_id=None,
                       provenance=provenance, payload={"key": key})
        for waiting in selected:
            dependencies.add_dependency(
                waiting, node.id, actor_id=None, provenance=provenance, db=connection,
            )
        return node.id


def settle_user_step(db, node_id: str, *, evidence: str, provenance: str) -> bool:
    """Settle the person's step as done; False when it had already ended."""
    from plans import review_evidence, user_settle

    with transaction(db) as connection:
        node = service._node_row(connection, node_id)
        if node["archived_at"] is not None or node["state"] in service._TERMINAL_STATES:
            return False
        common = {"db": connection, "actor_id": None, "provenance": provenance}
        user_settle.clear_stops(connection, node_id, common)
        if node["state"] == "pending":
            service.transition_node(node_id, "active", **common)
        if service._node_row(connection, node_id)["state"] == "active":
            review_evidence.submit(
                node_id, evidence=evidence, expected_revision=None,
                paused_plan_binds=False, **common,
            )
        else:
            review_evidence.record_verdict(node_id, evidence, verdict="Accepted", **common)
        service.transition_node(
            node_id, "accepted", verification_result="passed",
            verified_by=f"{VERIFIER_PREFIX}{provenance}", **common,
        )
        after.after_transition(node_id, "accepted", common)
        return True


def is_system_verifier(verifier: str | None) -> bool:
    """Whether a recorded verifier is a system settlement, not a thread's or the user's."""
    return bool(verifier) and str(verifier).startswith(VERIFIER_PREFIX)
