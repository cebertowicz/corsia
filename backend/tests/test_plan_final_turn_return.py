"""#2107: the worker's genuine final-Turn return, through delegation_harvest alone.

``on_thread_turn_complete`` is the one seam every harness's terminal handler
reaches (``core/plan_turn_end.record_turn_end_logged``, and the external
harness adapters). Before this fix it only ran the A11/D4 notice logic; the
actual return -- moving the parent's step to ``review_requested`` and writing
its one result-bearing wake -- waited for the next periodic continuation
sweep, up to ``CONTINUATION_SWEEP_INTERVAL`` seconds later (measured in
production far longer than that when the sweep loop lagged). These tests
assert the return exists immediately after ``record_turn_end`` alone, with no
separate sweep call, at every genuine terminal seam: success, error, a live
successor Turn that must still block it, and a replayed/duplicate terminal
signal that must not duplicate it. ``test_plan_harvest_wake.py`` continues to
own the underlying harvest-readiness contract itself (successor-Turn
exclusion, closed/failed labeling, idempotency); this file owns only the
Turn-end wiring.
"""

from __future__ import annotations

from automation.turn_end_effects import PendingTurnEndEffects
from automation import plan_liveness_terminal
import asyncio

import pytest

from automation import plan_liveness
from plans import delegation, mutations, notifications, service
from tests.plan_review_helpers import submit_review_evidence
from tests.test_plan_harvest_wake import (
    NOW,
    _db,
    _delegated,
    _end_turn,
    _facts,
    _running_turn,
    _wakes,
)


def _apply_turn_end(db, **outcome):
    """Apply the terminal hook in the test-owned transaction and commit it."""
    pending = PendingTurnEndEffects()
    plan_liveness_terminal.on_thread_turn_complete(
        "worker", db=db, effects=pending, **outcome,
    )
    db.commit()
    return pending


@pytest.mark.asyncio
async def test_a_completed_turn_returns_directly_with_no_separate_sweep(monkeypatch):
    """Success: the return is visible right after record_turn_end, unaided."""
    db = _db(monkeypatch)
    try:
        _parent_plan, delegated, _plan, (leaf,) = _delegated(db, "Write the tests")
        _running_turn(db, "turn-1")
        submit_review_evidence(leaf.id, db=db, evidence="Proof", actor_id="worker")
        db.commit()
        _end_turn(db, "turn-1", status="completed")

        _apply_turn_end(db)

        assert service.get_node(delegated.id, db=db).state == "review_requested"
        wakes = _wakes(db, "parent")
        assert len(wakes) == 1
        assert _facts(wakes[0])["returned"]["steps"][0]["id"] == leaf.id
    finally:
        db.close()


@pytest.mark.asyncio
async def test_an_errored_turn_returns_directly_with_no_separate_sweep(monkeypatch):
    """Error: F1's own Turn-error path still carries the return in the same hook."""
    db = _db(monkeypatch)
    try:
        _parent_plan, delegated, _plan, (leaf,) = _delegated(db, "Write the tests")
        _running_turn(db, "turn-1")
        submit_review_evidence(leaf.id, db=db, evidence="Proof", actor_id="worker")
        db.commit()
        _end_turn(db, "turn-1", status="error")

        _apply_turn_end(db, outcome="error")

        assert service.get_node(delegated.id, db=db).state == "review_requested"
        assert len(_wakes(db, "parent")) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_a_successor_turn_still_blocks_the_direct_return(monkeypatch):
    """A live successor Turn (FIFO continued the worker) defers the return.

    ``finish_turn`` settles ``turn-1`` and, in the same production sequence,
    a new Turn may already be claimed and running before this hook fires.
    That live successor must still make the harvest's own readiness check
    decline, exactly as it does for the periodic sweep -- calling the harvest
    directly at Turn end must not race ahead of that guard.
    """
    db = _db(monkeypatch)
    try:
        _parent_plan, delegated, _plan, (leaf,) = _delegated(db, "Write the tests")
        _running_turn(db, "turn-1")
        submit_review_evidence(leaf.id, db=db, evidence="Proof", actor_id="worker")
        db.commit()
        _end_turn(db, "turn-1", status="completed")
        _running_turn(db, "turn-2")  # the successor FIFO already claimed
        db.commit()

        _apply_turn_end(db)

        assert service.get_node(delegated.id, db=db).state == "active"
        assert _wakes(db, "parent") == []

        # Once the successor itself ends with nothing new, the return lands.
        _end_turn(db, "turn-2", status="completed")
        _apply_turn_end(db)
        assert service.get_node(delegated.id, db=db).state == "review_requested"
        assert len(_wakes(db, "parent")) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_turn_end_harvests_only_the_worker_that_ended(monkeypatch):
    """The hot Turn-end path follows one indexed attachment, never the global sweep."""
    db = _db(monkeypatch)
    try:
        _parent_plan, delegated, _plan, (leaf,) = _delegated(db, "Primary result")
        for thread_id, spawned_by in (
            ("other-parent", None),
            ("other-worker", "other-parent"),
        ):
            db.execute(
                """INSERT INTO threads
                   (id,user_id,project_id,spawned_by,model,created_at,updated_at)
                   VALUES (?,'user-1','project-1',?,'test/model','t','t')""",
                (thread_id, spawned_by),
            )
        other_parent_plan = service.ensure_plan("thread", "other-parent", db=db)
        other_delegated = service.add_node(
            other_parent_plan.id,
            kind="task",
            title="Other delegation",
            acceptance_criteria="Other result is reviewed",
            spawn_kind="claude-code",
            db=db,
        )
        service.transition_node(other_delegated.id, "active", db=db)
        other_plan = service.ensure_plan("thread", "other-worker", db=db)
        mutations.attach_spawned_thread(other_delegated.id, "other-worker", db=db)
        other_leaf = service.add_node(
            other_plan.id,
            kind="task",
            title="Other result",
            acceptance_criteria="Done",
            db=db,
        )
        service.transition_node(other_leaf.id, "active", actor_id="other-worker", db=db)
        submit_review_evidence(
            other_leaf.id,
            db=db,
            evidence="Other proof",
            actor_id="other-worker",
        )
        _running_turn(db, "turn-1")
        submit_review_evidence(
            leaf.id, db=db, evidence="Primary proof", actor_id="worker"
        )
        db.commit()
        _end_turn(db, "turn-1", status="completed")

        _apply_turn_end(db)

        assert service.get_node(delegated.id, db=db).state == "review_requested"
        assert service.get_node(other_delegated.id, db=db).state == "active"
        assert _wakes(db, "other-parent") == []
        assert delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[]) == [other_delegated.id]
    finally:
        db.close()



@pytest.mark.asyncio
async def test_a_replayed_terminal_signal_cannot_duplicate_the_return(monkeypatch):
    """Replay/restart: the same terminal delivered twice returns exactly once.

    A restart-recovered or re-delivered terminal calling this hook again for
    a Turn already harvested must find nothing left to do: one
    ``child_plan_completed`` event, one wake, one parent step transition.
    """
    db = _db(monkeypatch)
    try:
        _parent_plan, delegated, _plan, (leaf,) = _delegated(db, "Write the tests")
        _running_turn(db, "turn-1")
        submit_review_evidence(leaf.id, db=db, evidence="Proof", actor_id="worker")
        db.commit()
        _end_turn(db, "turn-1", status="completed")

        for _ in range(3):
            _apply_turn_end(db)

        assert service.get_node(delegated.id, db=db).state == "review_requested"
        assert len(_wakes(db, "parent")) == 1
        assert (
            db.execute(
                "SELECT COUNT(*) FROM plan_node_events "
                "WHERE node_id=? AND event='child_plan_completed'",
                (delegated.id,),
            ).fetchone()[0]
            == 1
        )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_the_parent_is_driven_only_after_the_harvest_commits(monkeypatch):
    """Parent-start ordering: the drive callback never sees an open write.

    ``on_thread_turn_complete`` runs the harvest inside its own transaction;
    ``record_turn_end`` must not start the parent's Turn until that write has
    committed, exactly as it already does for the plan-liveness wake.
    """
    db = _db(monkeypatch)
    try:
        _parent_plan, delegated, _plan, (leaf,) = _delegated(db, "Write the tests")
        _running_turn(db, "turn-1")
        submit_review_evidence(leaf.id, db=db, evidence="Proof", actor_id="worker")
        db.commit()
        _end_turn(db, "turn-1", status="completed")

        import infra.thread_execution as thread_execution

        seen_in_transaction: list[bool] = []
        seen_state: list[str] = []

        async def _record(thread_id: str) -> None:
            seen_in_transaction.append(db.in_transaction)
            seen_state.append(service.get_node(delegated.id, db=db).state)

        monkeypatch.setattr(thread_execution, "drive_thread", _record)

        effects = _apply_turn_end(db)
        await notifications.drive_threads(effects.drives)
        await asyncio.sleep(0)

        assert seen_in_transaction == [False]
        assert seen_state == ["review_requested"]
    finally:
        db.close()
