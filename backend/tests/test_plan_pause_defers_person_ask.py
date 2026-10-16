"""A whole-plan pause defers a raised decision's attention count until resume.

The decision stays raised and answerable meanwhile (the person may still answer
it), and resume restores the count. Node-level asks are covered by
``test_plan_chat_list_state`` and ``test_plan_operator_pause_disclosure``.
"""

from __future__ import annotations

from plans import boundary, decision_projection, operator_projection, service
from tests.test_plan_decision_answer import (
    _human,
    _mid,
    _raised_decision,
    db,  # noqa: F401 - shared fixture
)


def _lifecycle(db, plan_id: str, operation: str) -> None:
    boundary.mutate(
        plan_id, operation,
        {"expected_revision": service._plan_row(db, plan_id)["revision"]},
        actor=_human(), provenance=f"api.plans.{operation}", mutation_id=_mid(), db=db,
    )
    db.commit()


def _payload_awaiting(db) -> int:
    return operator_projection.read_thread("owner", actor=_human(), db=db)["attention"][
        "awaiting_count"
    ]


def test_pause_defers_the_decision_count_and_resume_restores_it(db):
    plan, _source, decision = _raised_decision(db)
    assert decision_projection.awaiting_count([plan.id], db=db) == 1
    assert _payload_awaiting(db) == 1

    _lifecycle(db, plan.id, "pause")

    assert decision_projection.awaiting_count([plan.id], db=db) == 0
    assert _payload_awaiting(db) == 0
    # Deferred, not removed: still raised and answerable.
    assert [item["id"] for item in decision_projection.raised_decisions([plan.id], db=db)] == [
        decision["id"]
    ]

    _lifecycle(db, plan.id, "resume")

    assert decision_projection.awaiting_count([plan.id], db=db) == 1
    assert _payload_awaiting(db) == 1
