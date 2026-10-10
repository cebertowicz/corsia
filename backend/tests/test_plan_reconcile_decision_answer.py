"""A decision answer reconciles the dependents it unblocks.

Answering a raised decision accepts its user review node. A task that depends
on that node becomes eligible in the same transaction, so the plan is
reconciled there and the dependent is selected with no `start` or next Turn.
"""

from __future__ import annotations

from plans import service
from tests.test_plan_decision_answer import _human, _raised_decision
from tests.test_plan_decisions import _mid, _task, db  # noqa: F401 - shared fixture


def _open_attempt(db, node_id: str):
    return db.execute(
        "SELECT * FROM plan_node_attempts WHERE node_id=? AND released_at IS NULL",
        (node_id,),
    ).fetchone()


def test_answer_selects_the_active_dependent_it_unblocks(db):
    from plans import decisions

    plan, _source, decision = _raised_decision(db)
    dependent = _task(plan.id, "Apply the chosen option", db=db)
    service.add_dependency(dependent.id, decision["node_id"], db=db)
    service.transition_node(dependent.id, "active", db=db)
    assert _open_attempt(db, dependent.id) is None

    decisions.answer(
        decision["id"], option_id="A", expected_revision=decision["revision"],
        mutation_id=_mid(), actor=_human(), provenance="api.plans.decision", db=db,
    )

    assert db.in_transaction is False
    assert service._node_row(db, decision["node_id"])["state"] == "accepted"
    selected = _open_attempt(db, dependent.id)
    assert selected is not None, "the answer left its unblocked dependent unselected"
    assert selected["executor_thread_id"] == "owner"
