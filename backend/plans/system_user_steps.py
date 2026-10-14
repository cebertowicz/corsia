"""System code opens and settles a step for the person in an agent's plan.

The route for a step no agent chose to ask for: the system saw the person
must act (a password page in the shared browser) and later saw it done. The
step is the user's (``assignee='user'``), attributed to the system
(``actor_id`` None plus the caller's provenance), never to the agent whose
plan holds it, so it never reads as the agent asking.

Opening is idempotent on the caller's key within that thread's plan, through
the plan's own mutation receipts (``plans.receipts``): one primary-key read,
however long the plan's history. The agent's selected task, if any, gains a dependency on the step, so the plan
carries the wait: settling or closing the step wakes that task through the
dependency path, and a paused plan stays paused until resume.

Settling accepts only a step this module opened (its receipt), so it
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
import plans.receipts as receipts
import plans.service as service
from plans.plan_write import transaction

#: The verifier prefix a system settlement records; the pane names CORSIA.
VERIFIER_PREFIX = "system:"
#: The receipt operation, and the mutation-id prefix that keys it in the plan.
OPERATION = "open_user_step"
_MUTATION_PREFIX = f"{VERIFIER_PREFIX}{OPERATION}:"
# A primary-key range seek over this module's receipts in one plan only.
_OPENED_SQL = """SELECT 1 FROM plan_mutation_receipts
    WHERE plan_id=? AND mutation_id>=? AND mutation_id<?
      AND operation=? AND json_extract(result_json,'$.node_id')=? LIMIT 1"""


def open_user_step(
    db, thread_id: str, *, key: str, title: str, description: str, provenance: str,
) -> str:
    """Open the person's step in ``thread_id``'s plan once per ``key``; return its id."""
    from plans import admission_attempts, dependencies

    key = str(key).strip()
    if not key:
        raise errors.PlanInputError("open_user_step needs a non-empty key")
    receipt = {
        "mutation_id": f"{_MUTATION_PREFIX}{key}",
        "actor_id": f"{VERIFIER_PREFIX}{provenance}",
        "operation": OPERATION,
        "fingerprint": receipts.request_fingerprint(OPERATION, {"key": key}),
    }
    with transaction(db) as connection:
        plan = service.ensure_plan("thread", thread_id, db=connection)
        existing = receipts.load(connection, plan_id=plan.id, **receipt)
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
        receipts.record(connection, plan_id=plan.id, result={"node_id": node.id}, **receipt)
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
        opened = connection.execute(_OPENED_SQL, _opened_params(node["plan_id"], node_id)).fetchone()
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


def _opened_params(plan_id: str, node_id: str) -> tuple:
    return (plan_id, _MUTATION_PREFIX, _MUTATION_PREFIX[:-1] + ";", OPERATION, node_id)


def is_system_verifier(verifier: str | None) -> bool:
    """Whether a recorded verifier is a system settlement, not a thread's or the user's."""
    return bool(verifier) and str(verifier).startswith(VERIFIER_PREFIX)
