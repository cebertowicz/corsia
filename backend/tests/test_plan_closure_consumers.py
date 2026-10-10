"""What `closed` means to the code that reads terminal states.

A new terminal state is only as safe as the consumers that enumerate the old
ones. Each case here pins one deliberate disposition, so a later change to it is
a visible decision rather than drift:

* closing a delegated step whose reachable child still has open work closes
  that work too (#1206): "no longer wanted" must not need ``failed`` to end
  a delegation, and the child's steps read closed, never failed;
* closing a delegated step whose child has finished *is* the verdict;
* ``closed`` reaches the exact read and node events only -- no milestone or
  delegated-terminal transcript fact, because it is not an outcome to announce;
* liveness counts a close as progress through the drop in open work, never as a
  settled event: a close needs only a reason, and counting it would let an
  add-then-close loop refresh the stall deadline forever.
"""

from __future__ import annotations

from automation.continuation.progress import ADVANCED, classify_progress, ProgressSnapshot
from plans import plan_events, projections, service
from plans.boundary import mutate
from tests.test_plan_archival import (  # noqa: F401 - db and raise_decision are reused fixtures
    OWNER,
    _delegated,
    _mid,
    _plan,
    _row,
    _task,
    db,
)


def _close(db, plan_id, node_id, reason="no longer wanted"):
    return mutate(
        plan_id, "close_node",
        {
            "node_id": node_id, "reason": reason,
            "expected_revision": _row(db, node_id)["revision"],
        },
        actor=OWNER, provenance="test", mutation_id=_mid(), db=db,
    )


def _child_step(db, child="child"):
    child_plan = service.ensure_plan("thread", child, db=db)
    step = _task(db, child_plan.id, "Child work")
    db.commit()
    return child_plan, step


# --- delegation --------------------------------------------------------------


def test_closing_a_step_whose_child_is_still_working_closes_that_work_too(db):
    plan, node = _delegated(db)
    _child_plan, step = _child_step(db)

    _close(db, plan.id, node.id)

    for closed in (node, step):
        assert _row(db, closed.id)["state"] == "closed"
        assert _row(db, closed.id)["archived_at"] is None


def test_closing_a_step_whose_child_has_finished_is_the_verdict(db):
    plan, node = _delegated(db)
    _child_plan, step = _child_step(db)
    service.transition_node(step.id, "active", db=db)
    service.transition_node(step.id, "failed", db=db)
    db.commit()

    _close(db, plan.id, node.id)

    assert _row(db, node.id)["state"] == "closed"
    assert _row(db, node.id)["archived_at"] is None


# --- transcript facts --------------------------------------------------------


def test_closing_announces_nothing_outside_the_node_history(db, monkeypatch):
    announced = []
    monkeypatch.setattr(
        plan_events, "append_owner_event",
        lambda connection, **kwargs: announced.append(kwargs["event_type"]),
    )
    plan = _plan(db)
    milestone = _task(db, plan.id, "Top-level milestone")

    _close(db, plan.id, milestone.id)

    assert announced == []


def test_the_terminal_projector_is_silent_for_closed(db):
    plan = _plan(db)
    node = _task(db, plan.id, "Milestone")

    assert plan_events.append_node_transition(
        db, node_id=node.id, event_id="e1", actor_id=None,
        new_state=service.CLOSED, created_at=service._now(),
    ) is None


# --- liveness ----------------------------------------------------------------


def test_a_close_is_progress_by_open_count_not_by_settled_events(db):
    plan = _plan(db)
    kept = _task(db, plan.id, "Kept")
    dropped = _task(db, plan.id, "Dropped")
    service.transition_node(kept.id, "active", db=db)
    db.commit()
    before = projections.liveness_signal("root", db=db)

    _close(db, plan.id, dropped.id)
    after = projections.liveness_signal("root", db=db)

    assert after.settled_events == before.settled_events
    assert after.count < before.count
    snapshot = ProgressSnapshot(before.count, before.signal_hash, before.settled_events)
    assert classify_progress(after, snapshot) == ADVANCED
