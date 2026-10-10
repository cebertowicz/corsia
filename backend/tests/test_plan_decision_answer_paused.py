"""A person's answer to a raised decision is accepted while its plan is paused.

Pausing stops the agent's work; it does not stop the user answering a question
the agent asked. The answer settles the decision, reaches the agent as an input
(pause contract rule 3), and leaves the plan paused.
"""

from __future__ import annotations

import pytest

from plans import boundary, decisions, review_evidence, service
from plans.pause_authority import reaches_paused_agent
from tests.test_plan_decision_answer import (
    _human,
    _mid,
    _raised_decision,
    db,  # noqa: F401 - shared fixture
)


def _pause(db, plan_id: str) -> None:
    boundary.mutate(
        plan_id, "pause",
        {"expected_revision": service._plan_row(db, plan_id)["revision"]},
        actor=_human(), provenance="api.plans.pause", mutation_id=_mid(), db=db,
    )
    db.commit()
    assert service._plan_row(db, plan_id)["state"] == "paused"


def test_answer_settles_the_decision_and_leaves_the_plan_paused(db):
    plan, _source, decision = _raised_decision(db)
    _pause(db, plan.id)

    answered = decisions.answer(
        decision["id"], option_id="A", expected_revision=decision["revision"],
        mutation_id=_mid(), actor=_human(), provenance="api.plans.decision", db=db,
    ).result["decision"]
    db.commit()

    assert answered["state"] == "answered"
    assert service._node_row(db, decision["node_id"])["state"] == "accepted"
    assert service._plan_row(db, plan.id)["state"] == "paused"
    resumed = db.execute(
        "SELECT 1 FROM plan_node_events WHERE event LIKE '%resume%'",
    ).fetchall()
    assert resumed == []


def test_answer_input_reaches_the_paused_agent(db):
    plan, _source, decision = _raised_decision(db)
    _pause(db, plan.id)

    decisions.answer(
        decision["id"], option_id="A", expected_revision=decision["revision"],
        mutation_id=_mid(), actor=_human(), provenance="api.plans.decision", db=db,
    )
    db.commit()

    row = db.execute(
        "SELECT * FROM messages WHERE id=?",
        (f"plan-decision-answer:{decision['id']}",),
    ).fetchone()
    assert row["thread_id"] == "owner"
    assert row["delivery_state"] == "queued"
    assert reaches_paused_agent(row)


def test_an_agent_still_cannot_request_review_on_a_paused_plan(db):
    plan, source, _decision = _raised_decision(db)
    service.transition_node(source.id, "active", actor_id="owner", provenance="t", db=db)
    db.commit()
    _pause(db, plan.id)

    with pytest.raises(service.PlanError):
        review_evidence.submit(
            source.id, evidence="done", expected_revision=None,
            actor_id="owner", provenance="tool.plans", db=db,
        )
