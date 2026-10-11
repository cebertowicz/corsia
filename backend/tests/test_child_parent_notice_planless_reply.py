"""#282: a planless worker's final reply is its return (A11, G2).

A worker that ends a clean Turn with no step on its plan and a final reply has
returned that reply: the parent's step moves to review through the harvest
and the parent gets the one "has returned" wake. Only a Turn that said
nothing is "returned nothing", and a Turn that errored is never either.
"""

from __future__ import annotations

import json

from automation import plan_liveness_terminal
from automation.turn_end_effects import PendingTurnEndEffects
from plans import delegation_harvest, service
from tests.child_parent_notice_helpers import begin_worker_turn, db, delegated, wakes

__all__ = ["db"]

_RESULT = "The audit is done: 3 findings, all fixed in PR 12."


def _complete(db, **kwargs):
    return plan_liveness_terminal.on_thread_turn_complete(
        "worker", db=db, effects=PendingTurnEndEffects(), **kwargs,
    )


def _worker_says(db, content: str) -> None:
    from infra.queries import insert_message

    insert_message("worker", "assistant", content, {"turn_id": "turn-1"}, db=db)


def _kinds(db) -> list[str]:
    return [str(row["id"]).split(":")[1] for row in wakes(db)]


def test_empty_plan_with_a_reply_returns_the_reply_for_review(db):
    step, _plan, _node = delegated(db, with_worker_node=False)
    begin_worker_turn(db)
    _worker_says(db, _RESULT)

    _complete(db)

    assert _kinds(db) == ["child_returned"]
    node = service.get_node(step.id, db=db)
    assert node.state == "review_requested"
    row = db.execute("SELECT * FROM plan_nodes WHERE id=?", (step.id,)).fetchone()
    assert delegation_harvest.awaiting_owner_verdict(db, row)
    returned = json.loads(str(wakes(db)[0]["content"]).splitlines()[-1])["returned"]
    assert returned["steps"][0]["evidence"] == _RESULT
    assert returned["total_count"] == 1
    assert "returned nothing" not in str(wakes(db)[0]["content"]).lower()


def test_the_reply_return_is_told_once_across_repeated_turn_ends(db):
    delegated(db, with_worker_node=False)
    begin_worker_turn(db)
    _worker_says(db, _RESULT)

    _complete(db)
    _complete(db)

    assert _kinds(db) == ["child_returned"]


def test_empty_plan_with_no_reply_is_still_returned_nothing(db):
    step, _plan, _node = delegated(db, with_worker_node=False)
    begin_worker_turn(db)

    _complete(db)

    assert _kinds(db) == ["child_returned_nothing"]
    assert service.get_node(step.id, db=db).state == "active"


def test_a_reply_from_an_earlier_turn_is_not_this_turns_return(db):
    step, _plan, _node = delegated(db, with_worker_node=False)
    _worker_says(db, _RESULT)
    begin_worker_turn(db, started_at="2999-01-01T00:00:00")

    _complete(db)

    assert _kinds(db) == ["child_returned_nothing"]
    assert service.get_node(step.id, db=db).state == "active"


def test_an_errored_turn_without_a_turn_id_is_never_returned_nothing(db):
    """The terminal hook, not the caller's turn id, decides a Turn errored."""
    step, _plan, _node = delegated(db, with_worker_node=False)

    _complete(db, outcome="error", error="Error calling LLM: quota has been exhausted")

    assert "child_returned_nothing" not in _kinds(db)
    assert service.get_node(step.id, db=db).state == "active"


def test_the_parent_can_accept_a_reply_return(db):
    """The reply return is a real review: the parent's verdict settles the step."""
    import uuid

    from plans.boundary import PlanActor, mutate
    from tests.plan_review_helpers import submit_review_evidence

    step, _plan, _node = delegated(db, with_worker_node=False)
    begin_worker_turn(db)
    _worker_says(db, _RESULT)
    _complete(db)
    submit_review_evidence(step.id, db=db, actor_id="parent")

    mutate(
        step.plan_id, "accept",
        {
            "node_id": step.id, "verification_result": "passed",
            "expected_revision": service._node_row(db, step.id)["revision"],
        },
        actor=PlanActor("parent", "user-1", "project-1"),
        provenance="test.verdict", mutation_id=uuid.uuid4().hex, db=db,
    )

    assert service.get_node(step.id, db=db).state == "accepted"
