"""A whole-plan pause defers a raised decision's attention until resume.

Activity's count, the operator payload and the dock queue share one rule.

The decision stays raised and answerable meanwhile (the person may still answer
it), and resume restores the count. A raised artifact ask on a plan's node
defers the same way. Human-review asks are covered by
``test_plan_chat_list_state`` and ``test_plan_operator_pause_disclosure``.
"""

from __future__ import annotations

from api import attention
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


def _dock(db) -> list:
    return attention._queue_for("user-1", db=db)


def test_pause_defers_the_decision_count_and_resume_restores_it(db):
    plan, _source, decision = _raised_decision(db)
    assert decision_projection.awaiting_count([plan.id], db=db) == 1
    assert _payload_awaiting(db) == 1
    assert len(_dock(db)) == 1

    _lifecycle(db, plan.id, "pause")

    assert decision_projection.awaiting_count([plan.id], db=db) == 0
    assert _payload_awaiting(db) == 0
    assert _dock(db) == []
    # Deferred, not removed: still raised and answerable.
    assert [item["id"] for item in decision_projection.raised_decisions([plan.id], db=db)] == [
        decision["id"]
    ]

    _lifecycle(db, plan.id, "resume")

    assert decision_projection.awaiting_count([plan.id], db=db) == 1
    assert _payload_awaiting(db) == 1
    assert len(_dock(db)) == 1


def test_pause_defers_a_raised_artifact_ask_and_resume_restores_it(db):
    from plans import artifact_links, operator_chat_state
    from registry import attention, models
    from tests.test_plan_decisions import _task

    plan = service.ensure_plan("thread", "cousin", db=db)
    node = _task(plan.id, "Merge the release", db=db)
    models.register_artifact(
        integration="github", type="pr", external_id="cousin#1",
        parent_thread_id="cousin", title="Merge the release", db=db,
    )
    raised = db.execute(
        "SELECT id FROM artifact_identities WHERE external_id='cousin#1'",
    ).fetchone()["id"]
    attention.raise_attention(raised, db=db)
    artifact_links.link(node.id, raised, "attention", actor_id=None, provenance="test", db=db)
    db.commit()

    def items() -> list:
        states = operator_chat_state.read_thread_states(["cousin"], actor=_human(), db=db)
        return states["states"]["cousin"]["attention_items"]

    assert [item["node_id"] for item in items()] == [node.id]
    _lifecycle(db, plan.id, "pause")
    assert items() == []
    _lifecycle(db, plan.id, "resume")
    assert [item["node_id"] for item in items()] == [node.id]
