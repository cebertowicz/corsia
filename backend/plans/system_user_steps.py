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

Settling accepts only a step this module opened (its ``OPENED`` event), so it
can never mark an agent's own work done. Such a step has no parent, which is
why the owner's top-level transition notice always reaches the agent. It is
the person's Mark done, observed by the system, through the same writer (``plans.user_settle.settle``): open rows beneath settle first, the
step's stops end, and questions drafted from it are withdrawn. It never
refuses on a pause or hold, the person's own pause included, because the
person has done the step. A step already settled, closed or archived is left
alone. The row reads ``Marked by CORSIA`` (``plans.closure.settled_reasons``).

Both join the caller's write transaction when there is one.
"""

from __future__ import annotations

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
        # Only the plan's person steps are probed for the event, so the read
        # stays small however long the agent's own history grows.
        existing = connection.execute(
            """SELECT e.node_id FROM plan_nodes n JOIN plan_node_events e ON e.node_id=n.id
               WHERE n.plan_id=? AND n.assignee='user' AND e.event=?
                 AND json_extract(e.payload_json,'$.key')=?
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
    from plans import user_settle

    with transaction(db) as connection:
        node = service._node_row(connection, node_id)
        opened = connection.execute(
            "SELECT 1 FROM plan_node_events WHERE node_id=? AND event=? LIMIT 1",
            (node_id, OPENED),
        ).fetchone()
        if opened is None:
            raise errors.PlanInputError("settle_user_step settles only a step open_user_step opened")
        if node["archived_at"] is not None or node["state"] in service._TERMINAL_STATES:
            return False
        user_settle.settle(
            "mark_done",
            {"node_id": node_id, "expected_revision": node["revision"]},
            f"{VERIFIER_PREFIX}{provenance}",
            {"db": connection, "actor_id": None, "provenance": provenance},
            evidence=evidence,
        )
        return True


def is_system_verifier(verifier: str | None) -> bool:
    """Whether a recorded verifier is a system settlement, not a thread's or the user's."""
    return bool(verifier) and str(verifier).startswith(VERIFIER_PREFIX)
