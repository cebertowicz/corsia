"""Matrix row D4: a parent waits quietly on a child that is still working.

While the worker on a delegated step has a running Turn or open steps of its
own, the parent's step reads as waiting on that worker, so liveness has
nothing to wake the parent for. The worker's return still wakes the parent
once: the harvest on a first return, and one wake per later round the worker
finishes after that return (the parent sent it back by message instead of
rejecting).
"""

from __future__ import annotations

from automation.turn_end_effects import PendingTurnEndEffects
from automation import plan_liveness_terminal
from datetime import datetime, timezone

from automation import plan_liveness
from plans import admission, delegation, service, wait_causes
from plans.projections import liveness_signal
from tests.child_parent_notice_helpers import db, delegated, wakes
from tests.plan_review_helpers import submit_review_evidence

__all__ = ["db"]

_BEFORE_ANY_HARVEST = "2000-01-01T00:00:00"


def _now_naive() -> str:
    """A Turn start as the Turn runtime writes it: naive UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _turn(db, turn_id: str, *, status: str, started_at: str) -> None:
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES (?,'worker',0,'native/model',?,?,'boot-1')""",
        (turn_id, status, started_at),
    )


def _returned(db):
    """A worker that returned its only step, harvested into the parent's review."""
    step, _plan, node = delegated(db)
    service.transition_node(node.id, "active", actor_id="worker", db=db)
    submit_review_evidence(node.id, db=db, actor_id="worker")
    assert delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[]) == [step.id]
    return step


def _child_cause(db, node_id: str) -> list[str]:
    return [
        cause["reason_key"]
        for cause in admission.project(node_id, db=db)["causes"]
    ]


def test_d4_returned_child_sent_back_by_message_keeps_the_parent_waiting(db):
    """The live case: the child plan still reads settled, but its Turn runs."""
    step = _returned(db)
    _turn(db, "turn-round", status="running", started_at=_now_naive())

    assert wait_causes.DELEGATED_CHILD in _child_cause(db, step.id)
    assert liveness_signal("parent", db=db).count == 0


def test_d4_child_mid_turn_before_planning_keeps_the_parent_waiting(db):
    """#1862: a delegated child mid-Turn with nothing planned yet."""
    step, _plan, _node = delegated(db, with_worker_node=False)
    _turn(db, "turn-1", status="running", started_at=_now_naive())

    assert _child_cause(db, step.id) == [wait_causes.DELEGATED_CHILD]
    assert liveness_signal("parent", db=db).count == 0


def test_d4_child_with_open_steps_between_turns_keeps_the_parent_waiting(db):
    """Open steps below are the child's to move; its own liveness wakes it."""
    step, _plan, _node = delegated(db)

    assert _child_cause(db, step.id) == [wait_causes.DELEGATED_CHILD]
    assert liveness_signal("parent", db=db).count == 0
    plan_liveness.on_turn_complete("parent", db=db)
    assert db.execute("SELECT * FROM plan_liveness_wakes").fetchall() == []


def test_d4_a_child_with_nothing_running_or_open_does_not_hold_the_step(db):
    """An empty, idle child is not working: the step is the parent's again."""
    step, _plan, _node = delegated(db, with_worker_node=False)

    assert wait_causes.DELEGATED_CHILD not in _child_cause(db, step.id)
    assert liveness_signal("parent", db=db).count == 1


def test_d4_a_child_step_it_cannot_move_does_not_hold_the_step(db):
    """A held or user-assigned step is not the child's to move."""
    from plans import holds

    step, _plan, node = delegated(db)
    holds.pause(
        node.id, reason="Set aside",
        expected_revision=service.get_node(node.id, db=db).revision,
        actor_id="worker", provenance="test", db=db,
    )

    assert wait_causes.DELEGATED_CHILD not in _child_cause(db, step.id)


def test_d4_a_user_assigned_child_step_does_not_hold_the_step(db):
    step, _plan, node = delegated(db)
    db.execute("UPDATE plan_nodes SET assignee='user' WHERE id=?", (node.id,))

    assert wait_causes.DELEGATED_CHILD not in _child_cause(db, step.id)


def test_d4_the_round_after_a_return_wakes_the_parent_once(db):
    """The round's end is the return: one wake per finished round, no repeat."""
    step = _returned(db)
    before = len(wakes(db))
    _turn(db, "turn-round", status="completed", started_at=_now_naive())

    effects = PendingTurnEndEffects()
    plan_liveness_terminal.on_thread_turn_complete("worker", db=db, effects=effects)
    plan_liveness_terminal.on_thread_turn_complete("worker", db=db, effects=effects)

    new = wakes(db)[before:]
    assert [row["id"] for row in new] == ["plan-wake:child_round_returned:turn-round:0"]
    line = str(new[0]["content"]).split("\n", 1)[0]
    assert "Delegate work" in line
    assert "awaits your verdict" in line
    assert service.get_node(step.id, db=db).state == "review_requested"


def test_d4_a_round_left_with_only_a_held_step_is_over_and_wakes(db):
    """PR #2011 review: waiting and round-end read one movable-work rule.

    A held step is not the child's to move, so the parent is not waiting on
    it -- and the round is over, so the parent is woken rather than left
    neither waiting nor told.
    """
    from plans import holds

    step = _returned(db)
    child_plan = service.ensure_plan("thread", "worker", db=db)
    stuck = service.add_node(
        child_plan.id, kind="task", title="Stuck", acceptance_criteria="-", db=db,
    )
    holds.pause(
        stuck.id, reason="Set aside", expected_revision=stuck.revision,
        actor_id="worker", provenance="test", db=db,
    )
    before = len(wakes(db))
    _turn(db, "turn-held", status="completed", started_at=_now_naive())

    effects = PendingTurnEndEffects()
    plan_liveness_terminal.on_thread_turn_complete("worker", db=db, effects=effects)

    assert wait_causes.DELEGATED_CHILD not in _child_cause(db, step.id)
    assert [row["id"] for row in wakes(db)[before:]] == [
        "plan-wake:child_round_returned:turn-held:0",
    ]


def test_d4_the_turn_the_harvest_already_reported_is_not_a_second_wake(db):
    """A harvest that ran mid-Turn already carried that Turn's return."""
    _returned(db)
    before = len(wakes(db))
    _turn(db, "turn-final", status="completed", started_at=_BEFORE_ANY_HARVEST)

    effects = PendingTurnEndEffects()
    plan_liveness_terminal.on_thread_turn_complete("worker", db=db, effects=effects)

    assert wakes(db)[before:] == []


def test_d4_a_reused_worker_wakes_the_parent_about_its_open_step(db):
    """#3285: a closed earlier step naming the same worker never eats the wake.

    Production shape: the parent closed the worker's first step (it stays on
    the plan) and gave the same worker a new one. The closed step sorts first,
    so a lookup that ignores state found it and dropped every later wake.
    """
    from plans import closure, mutations

    parent_plan = service.ensure_plan("thread", "parent", db=db)
    first, second = sorted(
        (
            service.add_node(
                parent_plan.id, kind="task", title="Delegate work",
                acceptance_criteria="Worker result is reviewed",
                spawn_kind="claude-code", db=db,
            )
            for _ in range(2)
        ),
        key=lambda node: node.id,
    )
    for node_id, title in ((first.id, "Earlier step"), (second.id, "Current step")):
        db.execute("UPDATE plan_nodes SET title=? WHERE id=?", (title, node_id))
    worker_plan = service.ensure_plan("thread", "worker", db=db)
    service.transition_node(first.id, "active", db=db)
    mutations.attach_spawned_thread(first.id, "worker", db=db)
    closure.close_node(first.id, reason="Replaced by a narrower ask", db=db)

    service.transition_node(second.id, "active", db=db)
    node = service.add_node(
        worker_plan.id, kind="task", title="Work", acceptance_criteria="Tests pass", db=db,
    )
    mutations.attach_spawned_thread(second.id, "worker", db=db)
    service.transition_node(node.id, "active", actor_id="worker", db=db)
    submit_review_evidence(node.id, db=db, actor_id="worker")
    assert delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[]) == [second.id]
    before = len(wakes(db))
    _turn(db, "turn-round", status="completed", started_at=_now_naive())

    plan_liveness_terminal.on_thread_turn_complete(
        "worker", db=db, effects=PendingTurnEndEffects(),
    )

    new = wakes(db)[before:]
    assert [row["id"] for row in new] == ["plan-wake:child_round_returned:turn-round:0"]
    assert "Current step" in str(new[0]["content"]).split("\n", 1)[0]


def test_d4_a_round_that_leaves_open_steps_is_not_over(db):
    """The child planned more work this round: no return until it finishes."""
    _step = _returned(db)
    plan = service.ensure_plan("thread", "worker", db=db)
    service.add_node(
        plan.id, kind="task", title="More", acceptance_criteria="Done", db=db,
    )
    before = len(wakes(db))
    _turn(db, "turn-round", status="completed", started_at=_now_naive())

    effects = PendingTurnEndEffects()
    plan_liveness_terminal.on_thread_turn_complete("worker", db=db, effects=effects)

    assert wakes(db)[before:] == []
