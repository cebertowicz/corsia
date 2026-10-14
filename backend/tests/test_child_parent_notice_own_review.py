"""Matrix section G: who acts on a delegated worker's steps (#3285).

The worker owns its steps until it returns; the parent judges what came back.
A review step in a worker's plan is the parent's when the latest return
handed it back, or when the worker has nothing left it can move (that review
*is* the return). Every other review step is the worker's own, so the step
behind it can move. A step that waits on the user holds the return until
the user acts.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from automation.turn_end_effects import PendingTurnEndEffects
from automation import plan_liveness_terminal
from plans import boundary, decisions, delegation, dependencies, service
from plans.boundary import PlanActor
from plans.projections import actionable_leaves, liveness_signal
from tests.child_parent_notice_helpers import db, delegated, wakes, worker_actor
from tests.plan_review_helpers import submit_review_evidence

__all__ = ["db"]

PARENT = PlanActor("parent", "user-1", "project-1")
USER = PlanActor(None, "user-1", "project-1", kind="user")


def _harvest(db) -> list[str]:
    return delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[])


def _actionable_ids(db, thread_id: str) -> list[str]:
    plan = db.execute(
        "SELECT * FROM plans WHERE owner_kind='thread' AND owner_id=?", (thread_id,),
    ).fetchone()
    return [str(row["id"]) for row in actionable_leaves(db, plan)[0]]


def _review_step_then_dependent(db):
    """G4's shape: A up for review, B waiting on A."""
    step, plan, a = delegated(db, title="A")
    b = service.add_node(plan.id, kind="task", title="B", acceptance_criteria="ok", db=db)
    dependencies.add_dependency(b.id, a.id, db=db)
    service.transition_node(a.id, "active", actor_id="worker", db=db)
    submit_review_evidence(a.id, db=db, actor_id="worker")
    return step, plan, a, b


def _review_of(db, thread_id: str, node_id: str) -> dict:
    from plans import operator_projection

    payload = operator_projection.read_thread(thread_id, actor=USER, db=db)
    nodes = [node for item in payload["plans"] for node in item["nodes"]]
    return next(node for node in nodes if node["id"] == node_id)["review"]


def test_g4_a_review_step_with_work_behind_it_is_the_workers_to_judge(db):
    """The original #3285 deadlock: both sides read nothing to do."""
    step, _plan, a, _b = _review_step_then_dependent(db)

    assert _actionable_ids(db, "worker") == [a.id]
    assert liveness_signal("parent", db=db).count == 0
    assert _harvest(db) == []
    assert service.get_node(step.id, db=db).state == "active"


def test_g4_the_workers_verdict_on_its_own_step_frees_the_next(db):
    _step, _plan, a, b = _review_step_then_dependent(db)

    boundary.mutate(
        _plan.id, "accept",
        {"node_id": a.id, "expected_revision": service.get_node(a.id, db=db).revision,
         "verification_result": "passed"},
        actor=worker_actor(), provenance="tool.plans", mutation_id="own-1", db=db,
    )

    assert service.get_node(a.id, db=db).state == "accepted"
    assert b.id in _actionable_ids(db, "worker")


def test_g4_a_last_step_up_for_review_is_the_return_not_the_workers(db):
    step, _plan, node = delegated(db)
    service.transition_node(node.id, "active", actor_id="worker", db=db)
    submit_review_evidence(node.id, db=db, actor_id="worker")

    assert _actionable_ids(db, "worker") == []
    assert _harvest(db) == [step.id]


def test_g4_a_returned_step_stays_the_parents_after_the_worker_adds_work(db):
    step, plan, node = delegated(db)
    service.transition_node(node.id, "active", actor_id="worker", db=db)
    submit_review_evidence(node.id, db=db, actor_id="worker")
    assert _harvest(db) == [step.id]

    service.add_node(plan.id, kind="task", title="More", acceptance_criteria="ok", db=db)

    assert node.id not in _actionable_ids(db, "worker")


def test_g4_a_later_rounds_last_review_is_that_rounds_return(db):
    """Not the worker's own: otherwise a round could never end."""
    step, plan, node = delegated(db)
    service.transition_node(node.id, "active", actor_id="worker", db=db)
    submit_review_evidence(node.id, db=db, actor_id="worker")
    assert _harvest(db) == [step.id]
    later = service.add_node(plan.id, kind="task", title="Round 2", acceptance_criteria="ok", db=db)
    service.transition_node(later.id, "active", actor_id="worker", db=db)
    submit_review_evidence(later.id, db=db, actor_id="worker")
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES ('turn-round','worker',0,'native/model','completed',?,'boot-1')""",
        (datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),),
    )
    before = len(wakes(db))

    plan_liveness_terminal.on_thread_turn_complete(
        "worker", db=db, effects=PendingTurnEndEffects(),
    )

    assert later.id not in _actionable_ids(db, "worker")
    assert [row["id"] for row in wakes(db)[before:]] == [
        "plan-wake:child_round_returned:turn-round:0",
    ]


def test_g5_the_row_names_who_reviews_each_step(db):
    step, plan, a, _b = _review_step_then_dependent(db)

    own = _review_of(db, "worker", a.id)
    assert own["actor"]["id"] == "worker"
    assert own["label"] == "Awaiting the agent's review"

    service.transition_node(_b.id, "active", actor_id="worker", db=db)
    submit_review_evidence(_b.id, db=db, actor_id="worker")
    assert _harvest(db) == [step.id]

    returned = _review_of(db, "worker", _b.id)
    assert returned["actor"]["id"] == "parent"
    assert returned["label"] == "Awaiting parent agent"


@pytest.mark.parametrize("operation", ["accept", "reject", "close_node"])
def test_g7_a_parent_acting_on_its_workers_step_is_told_its_own_step(db, operation):
    step, plan, node = delegated(db)
    service.transition_node(node.id, "active", actor_id="worker", db=db)
    submit_review_evidence(node.id, db=db, actor_id="worker")
    params = {
        "node_id": node.id,
        "expected_revision": service.get_node(node.id, db=db).revision,
        "verification_result": "passed", "feedback": "redo", "reason": "done",
    }
    params = {key: value for key, value in params.items() if key in {
        "accept": {"node_id", "expected_revision", "verification_result"},
        "reject": {"node_id", "expected_revision", "feedback"},
        "close_node": {"node_id", "expected_revision", "reason"},
    }[operation]}

    with pytest.raises(service.PlanInputError) as refused:
        boundary.mutate(
            plan.id, operation, params, actor=PARENT,
            provenance="tool.plans", mutation_id=f"cross-{operation}", db=db,
        )

    assert step.id in str(refused.value)
    assert service.get_node(node.id, db=db).state == "review_requested"


def _raised_decision(db, plan, node):
    drafted = decisions.draft(
        plan.id, source_node_id=node.id, mutation_id="d-1",
        actor=worker_actor(), provenance="tool.plans", db=db,
        title="Pick", context="c", tradeoff="t", recommendation="A", rationale="r",
        options=[{"id": "A", "label": "A"}, {"id": "B", "label": "B"}],
        other_enabled=True,
    )
    decision_id = drafted.result["decision"]["id"]
    reviewed = decisions.parent_review(
        decision_id, expected_revision=drafted.result["decision"]["revision"],
        mutation_id="r-1", actor=PARENT, provenance="tool.plans", db=db,
    )
    raised = decisions.raise_to_user(
        decision_id, expected_revision=reviewed.result["decision"]["revision"],
        mutation_id="u-1", actor=PARENT, provenance="tool.plans", db=db,
    )
    return decision_id, raised.result["decision"]["revision"]


def test_g10_a_return_waits_for_the_users_answer(db):
    step, plan, node = delegated(db)
    service.transition_node(node.id, "active", actor_id="worker", db=db)
    decision_id, revision = _raised_decision(db, plan, node)
    submit_review_evidence(node.id, db=db, actor_id="worker")

    assert _harvest(db) == []
    assert _actionable_ids(db, "worker") == []
    assert service.get_node(step.id, db=db).state == "active"

    decisions.answer(
        decision_id, option_id="A", expected_revision=revision,
        mutation_id="a-1", actor=USER, provenance="api.plans", db=db,
    )

    assert _harvest(db) == [step.id]


def test_g11_a_worker_cannot_assign_its_step_to_the_user(db):
    _step, plan, _node = delegated(db)

    with pytest.raises(service.PlanInputError) as refused:
        boundary.mutate(
            plan.id, "add",
            {"kind": "task", "title": "Ask Steve", "acceptance_criteria": "answered",
             "assignee": "user"},
            actor=worker_actor(), provenance="tool.plans", mutation_id="to-user", db=db,
        )

    assert "decision" in str(refused.value)


def test_g11_an_agent_with_no_parent_still_assigns_the_user(db):
    plan = service.ensure_plan("thread", "grandparent", db=db)

    boundary.mutate(
        plan.id, "add",
        {"kind": "task", "title": "Sign off", "acceptance_criteria": "signed",
         "assignee": "user"},
        actor=PlanActor("grandparent", "user-1", "project-1"),
        provenance="tool.plans", mutation_id="root-user", db=db,
    )


def _returned_then_restated(db, **edit):
    """G18's shape (#2962): the worker returned, then the parent edited its step."""
    from plans import mutations

    step, plan, node = delegated(db)
    service.transition_node(node.id, "active", actor_id="worker", db=db)
    submit_review_evidence(node.id, db=db, actor_id="worker")
    mutations.edit_node(step.id, db=db, **edit)
    return step, plan


def test_g18_a_return_older_than_the_steps_requirements_is_not_harvested(db):
    step, _plan = _returned_then_restated(db, acceptance_criteria="New criteria")

    assert _harvest(db) == []
    assert service.get_node(step.id, db=db).state == "active"


def test_g18_new_work_after_the_restatement_is_the_return(db):
    step, plan = _returned_then_restated(db, acceptance_criteria="New criteria")
    redo = service.add_node(
        plan.id, kind="task", title="Redo", acceptance_criteria="New criteria", db=db,
    )
    service.transition_node(redo.id, "active", actor_id="worker", db=db)
    submit_review_evidence(redo.id, db=db, actor_id="worker")

    assert _harvest(db) == [step.id]


def test_g18_a_new_title_alone_keeps_the_return(db):
    step, _plan = _returned_then_restated(db, title="Renamed")

    assert _harvest(db) == [step.id]
