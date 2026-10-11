"""The worker's final return is one result-bearing wake to its parent.

A worker returns by putting its last step up for review. That is the end of
its execution for the assignment: the parent's verdict is what comes next, and
the parent needs one input that carries the result, the identities and the
evidence references to give it -- not a review wake now and a liveness wake
later, with an acceptance required in between to make the second one fire.

So harvest fires when the delegated work has *returned* -- every step of the
worker's is settled or awaiting the verdict -- and appends exactly one wake per
attachment epoch. The worker is quiet while the parent reviews: a step awaiting
the parent's verdict is not work the worker could move. An intermediate review,
one the worker still has other work behind, reaches the parent as a passive
digest fact; a final one is the harvest's to carry.
"""

from __future__ import annotations

from automation.turn_end_effects import PendingTurnEndEffects
from automation import plan_liveness_terminal
import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest

from automation.continuation import runner
from automation.continuation.sources.harvest import HarvestSource
from infra.schema import SCHEMA_SQL
from plans import delegation, mutations, plan_wake, service
from plans.projections import liveness_signal
from tests.plan_review_helpers import submit_review_evidence
from tests.plan_writer_authorizer import raw_plan_seed

NOW = "2026-08-01T00:00:00+00:00"


def _running_turn(db, turn_id: str, thread_id: str = "worker") -> None:
    """Insert a live Turn row for ``thread_id``, as the runtime does at Turn start."""
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES (?,?,0,'test/model','running','2026-08-01T00:00:00','boot-1')""",
        (turn_id, thread_id),
    )


def _end_turn(db, turn_id: str, *, status: str = "completed") -> None:
    """End one Turn through the production terminal state seam."""
    from infra.thread_driver import finish_turn

    finish_turn(
        turn_id, outcome=status, ended_at="2026-08-01T00:05:00",
        successor_turn_id=f"{turn_id}-next", boot_epoch="boot-1", db=db,
    )


def _db(monkeypatch) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project-1','Project','t','t')"
    )
    for thread_id, spawned_by in (("parent", None), ("worker", "parent")):
        db.execute(
            """INSERT INTO threads
               (id,user_id,project_id,spawned_by,model,created_at,updated_at)
               VALUES (?,'user-1','project-1',?,'test/model','t','t')""",
            (thread_id, spawned_by),
        )
    db.commit()
    return db


def _delegated(db, *titles: str):
    """The parent's delegated step, and the worker's active steps under it."""
    parent_plan = service.ensure_plan("thread", "parent", db=db)
    delegated = service.add_node(
        parent_plan.id, kind="task", title="Delegate work",
        acceptance_criteria="Worker result is reviewed",
        spawn_kind="claude-code", db=db,
    )
    service.transition_node(delegated.id, "active", db=db)
    plan = service.ensure_plan("thread", "worker", db=db)
    mutations.attach_spawned_thread(delegated.id, "worker", db=db)
    leaves = []
    for title in titles:
        leaf = service.add_node(
            plan.id, kind="task", title=title, acceptance_criteria="Done", db=db,
        )
        service.transition_node(leaf.id, "active", actor_id="worker", db=db)
        leaves.append(leaf)
    db.commit()
    return parent_plan, delegated, plan, leaves


async def _sweep(db, deliver=None):
    deliver = deliver or AsyncMock(return_value=True)
    result = await runner.sweep(
        NOW, db=db, sources=[HarvestSource()],
        is_idle=lambda _thread: True, deliver=deliver,
    )
    await asyncio.sleep(0)
    return result, deliver


def _inputs(db, thread_id: str) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT id,content FROM messages WHERE thread_id=? ORDER BY seq,rowid",
        (thread_id,),
    ).fetchall()


def _wakes(db, thread_id: str) -> list[sqlite3.Row]:
    return [row for row in _inputs(db, thread_id) if row["id"].startswith("plan-wake:")]


def _facts(row) -> dict:
    return json.loads(row["content"].split("\n", 1)[1])


@pytest.mark.asyncio
async def test_the_workers_final_return_is_one_result_bearing_wake(monkeypatch):
    db = _db(monkeypatch)
    try:
        _parent_plan, delegated, plan, (leaf,) = _delegated(db, "Write the tests")
        submit_review_evidence(
            leaf.id, db=db, evidence="Tests pass on three runs", actor_id="worker",
        )
        db.commit()

        _result, deliver = await _sweep(db)

        assert service.get_node(delegated.id, db=db).state == "review_requested"
        wakes = _wakes(db, "parent")
        assert [row["id"] for row in wakes] == [
            f"plan-wake:child_returned:{delegated.id}:"
            f"{service.get_node(delegated.id, db=db).revision}",
        ]
        assert "Delegate work" in wakes[0]["content"].split("\n", 1)[0]
        returned = _facts(wakes[0])["returned"]
        assert returned["child_plan_id"] == plan.id
        assert returned["worker_thread_id"] == "worker"
        assert returned["steps"] == [{
            "id": leaf.id, "title": "Write the tests", "state": "review_requested",
            "evidence": "Tests pass on three runs", "artifact_ids": [],
        }]
        assert "next" in _facts(wakes[0])
        # The final review is the harvest's to carry, not a routed occurrence.
        deliver.assert_not_awaited()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_a_second_sweep_adds_no_second_wake(monkeypatch):
    db = _db(monkeypatch)
    try:
        _parent_plan, delegated, _plan, (leaf,) = _delegated(db, "Write the tests")
        submit_review_evidence(leaf.id, db=db, actor_id="worker")
        db.commit()

        await _sweep(db)
        await _sweep(db)

        assert len(_wakes(db, "parent")) == 1
        assert db.execute(
            "SELECT COUNT(*) FROM plan_node_events "
            "WHERE node_id=? AND event='child_plan_completed'",
            (delegated.id,),
        ).fetchone()[0] == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_the_worker_is_quiet_while_the_parent_reviews(monkeypatch):
    db = _db(monkeypatch)
    try:
        _parent_plan, _delegated_node, _plan, (leaf,) = _delegated(db, "Write the tests")
        submit_review_evidence(leaf.id, db=db, actor_id="worker")
        db.commit()

        await _sweep(db)

        assert liveness_signal("worker", db=db).count == 0
        assert _inputs(db, "worker") == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_an_intermediate_review_is_neither_harvested_nor_routed(monkeypatch):
    db = _db(monkeypatch)
    try:
        _parent_plan, delegated, _plan, (first, _second) = _delegated(
            db, "First step", "Second step",
        )
        submit_review_evidence(first.id, db=db, actor_id="worker")
        db.commit()

        _result, deliver = await _sweep(db)

        # A node's reviewer is the owner of the plan it lives in, so the worker
        # reviewing its own step is nobody else's to give: the parent is told,
        # not woken, and the delegation has not returned.
        deliver.assert_not_awaited()
        assert db.execute(
            "SELECT COUNT(*) FROM context_change_journal "
            "WHERE change_kind='plan.child_node_review_requested'"
        ).fetchone()[0] == 1
        assert service.get_node(delegated.id, db=db).state == "active"
        assert _wakes(db, "parent") == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_a_delegation_with_nothing_returned_is_not_harvested(monkeypatch):
    db = _db(monkeypatch)
    try:
        _parent_plan, delegated, plan, () = _delegated(db)
        # Something happened in the worker's plan since attachment, but no step
        # of it remains: there is no result to hand the parent.
        gone = service.add_node(
            plan.id, kind="task", title="Abandoned", acceptance_criteria="-", db=db,
        )
        with raw_plan_seed(db):
            db.execute("UPDATE plan_nodes SET archived_at='t' WHERE id=?", (gone.id,))
        db.commit()

        await _sweep(db)

        assert service.get_node(delegated.id, db=db).state == "active"
        assert _wakes(db, "parent") == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_the_parents_verdict_costs_the_worker_no_turn(monkeypatch):
    db = _db(monkeypatch)
    try:
        _parent_plan, delegated, plan, (leaf,) = _delegated(db, "Write the tests")
        submit_review_evidence(leaf.id, db=db, actor_id="worker")
        db.commit()
        await _sweep(db)

        service.transition_node(
            leaf.id, "accepted", actor_id="parent",
            verification_result="passed", verified_by="parent", db=db,
        )
        # What the boundary does when another actor closes the worker's task.
        plan_wake.notify_closed(
            db, leaf.id, plan_id=plan.id, executor="worker", causing_executor="parent",
        )
        submit_review_evidence(delegated.id, db=db, actor_id="parent")
        service.transition_node(
            delegated.id, "accepted", actor_id="parent",
            verification_result="passed", verified_by="parent", db=db,
        )
        db.commit()

        assert _wakes(db, "worker") == []
        assert all(
            json.loads(row["metadata"] or "{}").get("event_disposition") == "passive"
            for row in db.execute(
                "SELECT metadata FROM messages WHERE thread_id='worker'"
            ).fetchall()
        )
        assert service.get_node(delegated.id, db=db).state == "accepted"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_a_parent_that_cannot_take_the_wake_is_not_harvested_yet(monkeypatch):
    """The return is never lost or split: it waits, whole, until the parent can take it."""
    db = _db(monkeypatch)
    try:
        _parent_plan, delegated, _plan, (leaf,) = _delegated(db, "Write the tests")
        db.execute("UPDATE threads SET model=NULL WHERE id='parent'")
        submit_review_evidence(leaf.id, db=db, actor_id="worker")
        db.commit()

        _result, deliver = await _sweep(db)

        assert service.get_node(delegated.id, db=db).state == "active"
        assert _wakes(db, "parent") == []
        assert db.execute(
            "SELECT COUNT(*) FROM plan_node_events WHERE node_id=? AND event='child_plan_completed'",
            (delegated.id,),
        ).fetchone()[0] == 0
        deliver.assert_not_awaited()

        db.execute("UPDATE threads SET model='test/model' WHERE id='parent'")
        db.commit()
        _result, deliver = await _sweep(db)

        assert service.get_node(delegated.id, db=db).state == "review_requested"
        assert len(_wakes(db, "parent")) == 1
        assert _facts(_wakes(db, "parent")[0])["returned"]["steps"][0]["id"] == leaf.id
        deliver.assert_not_awaited()
    finally:
        db.close()


def test_a_workers_plan_is_its_own_again_once_the_delegated_step_is_archived(monkeypatch):
    """parent_plan_id outlives the delegation; liveness must not."""
    from plans import delegation_harvest, projections

    db = _db(monkeypatch)
    try:
        _parent_plan, delegated, plan, (leaf,) = _delegated(db, "Write the tests")
        row = service._plan_row(db, plan.id)
        assert projections.live_delegation(db, "worker", row["parent_plan_id"])

        with raw_plan_seed(db):
            db.execute(
                "UPDATE plan_nodes SET archived_at='t' WHERE id=?", (delegated.id,),
            )
        db.commit()

        assert not projections.live_delegation(db, "worker", row["parent_plan_id"])
        # The worker's later review is now its own to give: live work, not a wait.
        submit_review_evidence(leaf.id, db=db, actor_id="worker")
        db.commit()
        review = {"causes": [], "condition": "review", "conflict": None}
        leaf_row = db.execute("SELECT * FROM plan_nodes WHERE id=?", (leaf.id,)).fetchone()
        assert delegation_harvest.parent_reviews(db, row) == frozenset()
        assert projections._is_actionable(
            leaf_row, review, parent_reviews=delegation_harvest.parent_reviews(db, row),
        )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_a_wake_the_parent_has_nothing_left_to_act_on_rolls_the_harvest_back(monkeypatch):
    """Any refused wake -- not only a missing model -- leaves the epoch unharvested."""
    from plans import plan_wake

    db = _db(monkeypatch)
    try:
        _parent_plan, delegated, _plan, (leaf,) = _delegated(db, "Write the tests")
        submit_review_evidence(leaf.id, db=db, actor_id="worker")
        db.commit()
        real_append = plan_wake.append
        monkeypatch.setattr(plan_wake, "append", lambda *a, **k: None)

        await _sweep(db)

        assert service.get_node(delegated.id, db=db).state == "active"
        assert db.execute(
            "SELECT COUNT(*) FROM plan_node_events WHERE node_id=? AND event='child_plan_completed'",
            (delegated.id,),
        ).fetchone()[0] == 0

        monkeypatch.setattr(plan_wake, "append", real_append)
        await _sweep(db)

        assert service.get_node(delegated.id, db=db).state == "review_requested"
        assert len(_wakes(db, "parent")) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_the_return_wake_is_bounded_and_counts_what_it_omits(monkeypatch):
    from plans import plan_wake

    db = _db(monkeypatch)
    try:
        titles = [f"Step {i:02d}" for i in range(plan_wake.RETURNED_STEPS_MAX + 3)]
        _parent_plan, _delegated_node, _plan, leaves = _delegated(db, *titles)
        for leaf in leaves:
            submit_review_evidence(leaf.id, db=db, evidence="x" * 2000, actor_id="worker")
        db.commit()

        await _sweep(db)

        returned = _facts(_wakes(db, "parent")[0])["returned"]
        assert len(returned["steps"]) == plan_wake.RETURNED_STEPS_MAX
        assert returned["omitted_count"] == 3
        assert returned["total_count"] == len(titles)
        assert [step["title"] for step in returned["steps"]] == titles[: plan_wake.RETURNED_STEPS_MAX]
        assert all(
            len(step["evidence"].encode()) <= plan_wake.RETURNED_EVIDENCE_MAX_BYTES
            for step in returned["steps"]
        )
    finally:
        db.close()


def _two_level(db):
    """Top parent's step A -> middle worker -> its step B -> leaf worker's step."""
    db.execute(
        """INSERT INTO threads
           (id,user_id,project_id,spawned_by,model,created_at,updated_at)
           VALUES ('leaf','user-1','project-1','worker','test/model','t','t')"""
    )
    _parent_plan, step_a, middle_plan, () = _delegated(db)
    step_b = service.add_node(
        middle_plan.id, kind="task", title="Delegated verify",
        acceptance_criteria="Leaf result is judged", spawn_kind="claude-code", db=db,
    )
    service.transition_node(step_b.id, "active", actor_id="worker", db=db)
    leaf_plan = service.ensure_plan("thread", "leaf", db=db)
    mutations.attach_spawned_thread(step_b.id, "leaf", db=db)
    leaf_step = service.add_node(
        leaf_plan.id, kind="task", title="Leaf work", acceptance_criteria="Done", db=db,
    )
    service.transition_node(leaf_step.id, "active", actor_id="leaf", db=db)
    db.commit()
    return step_a, step_b, leaf_step


@pytest.mark.asyncio
async def test_a_leafs_return_is_not_the_middle_workers_return_until_it_judges(monkeypatch):
    """The top parent hears once, after the middle worker acts, with its evidence (#2085)."""
    db = _db(monkeypatch)
    try:
        step_a, step_b, leaf_step = _two_level(db)
        submit_review_evidence(leaf_step.id, db=db, evidence="Leaf proof", actor_id="leaf")
        db.commit()

        # Two sweeps: whether both harvests share a write depends on row order.
        await _sweep(db)
        await _sweep(db)

        assert service.get_node(step_b.id, db=db).state == "review_requested"
        assert service.get_node(step_a.id, db=db).state == "active"
        assert db.execute(
            "SELECT COUNT(*) FROM plan_node_events "
            "WHERE node_id=? AND event='child_plan_completed'", (step_a.id,),
        ).fetchone()[0] == 0
        assert _wakes(db, "parent") == []
        assert len(_wakes(db, "worker")) == 1

        submit_review_evidence(step_b.id, db=db, evidence="Verified leaf", actor_id="worker")
        db.commit()
        await _sweep(db)
        await _sweep(db)

        assert service.get_node(step_a.id, db=db).state == "review_requested"
        wakes = _wakes(db, "parent")
        assert len(wakes) == 1
        (returned,) = _facts(wakes[0])["returned"]["steps"]
        assert returned["id"] == step_b.id
        assert returned["evidence"] == "Verified leaf"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_a_sweep_inside_the_workers_live_turn_does_not_early_harvest(monkeypatch):
    """#2107: a transient all-settled shape inside a running worker Turn is not the return.

    The worker settles its first step -- momentarily leaving its plan with no
    open leaf -- while its own Turn is still running. A sweep at that instant
    must not harvest: not transition the parent node, not append
    ``child_plan_completed``, not write a return wake, and not consume the
    attachment. Only once the Turn is no longer running does the worker's
    real return -- its second step -- reach the parent, exactly once.
    """
    db = _db(monkeypatch)
    try:
        _parent_plan, delegated, plan, () = _delegated(db)
        _running_turn(db, "turn-live")
        step1 = service.add_node(
            plan.id, kind="task", title="Setup step 1", acceptance_criteria="Done", db=db,
        )
        service.transition_node(step1.id, "active", actor_id="worker", db=db)
        submit_review_evidence(step1.id, db=db, evidence="Setup proof", actor_id="worker")
        service.transition_node(
            step1.id, "accepted", actor_id="worker", verification_result="passed",
            verified_by="worker", db=db,
        )
        db.commit()

        # Momentarily all-settled shape, but the worker's Turn is still running.
        _result, deliver = await _sweep(db)
        assert service.get_node(delegated.id, db=db).state == "active"
        assert _wakes(db, "parent") == []
        assert db.execute(
            "SELECT COUNT(*) FROM plan_node_events "
            "WHERE node_id=? AND event='child_plan_completed'", (delegated.id,),
        ).fetchone()[0] == 0
        deliver.assert_not_awaited()

        # A second sweep during the same live Turn changes nothing either.
        await _sweep(db)
        assert service.get_node(delegated.id, db=db).state == "active"
        assert _wakes(db, "parent") == []

        step2 = service.add_node(
            plan.id, kind="task", title="Step 2", acceptance_criteria="Done", db=db,
        )
        service.transition_node(step2.id, "active", actor_id="worker", db=db)
        submit_review_evidence(step2.id, db=db, evidence="Step 2 proof", actor_id="worker")
        db.commit()

        _end_turn(db, "turn-live")
        from automation import plan_liveness

        plan_liveness_terminal.on_thread_turn_complete(
            "worker", db=db, effects=PendingTurnEndEffects(),
        )
        db.commit()

        await _sweep(db)

        assert service.get_node(delegated.id, db=db).state == "review_requested"
        wakes = _wakes(db, "parent")
        assert len(wakes) == 1
        returned = _facts(wakes[0])["returned"]
        assert [row["id"] for row in returned["steps"]] == [step1.id, step2.id]
        assert returned["steps"][1]["evidence"] == "Step 2 proof"

        # Idempotent: a repeated sweep after the real return adds nothing more.
        await _sweep(db)
        assert len(_wakes(db, "parent")) == 1
        assert db.execute(
            "SELECT COUNT(*) FROM plan_node_events "
            "WHERE node_id=? AND event='child_plan_completed'", (delegated.id,),
        ).fetchone()[0] == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_an_errored_turn_still_lets_the_return_harvest(monkeypatch):
    """A Turn ending in error is no longer 'running': its return is not silenced."""
    db = _db(monkeypatch)
    try:
        _parent_plan, delegated, plan, (leaf,) = _delegated(db, "Write the tests")
        _running_turn(db, "turn-err")
        submit_review_evidence(leaf.id, db=db, evidence="Proof", actor_id="worker")
        db.commit()

        # Still running: not harvested yet.
        await _sweep(db)
        assert service.get_node(delegated.id, db=db).state == "active"

        # The Turn terminal settles the row to 'error' before any sweep runs.
        _end_turn(db, "turn-err", status="error")
        from automation import plan_liveness

        plan_liveness_terminal.on_thread_turn_complete(
            "worker", db=db, outcome="error",
            effects=PendingTurnEndEffects(),
        )
        db.commit()

        await _sweep(db)

        assert service.get_node(delegated.id, db=db).state == "review_requested"
        assert len(_wakes(db, "parent")) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_a_direct_harvest_with_no_active_turn_row_still_works(monkeypatch):
    """Manual/no-Turn sweeps (no ``active_turns`` row at all) are unaffected."""
    db = _db(monkeypatch)
    try:
        _parent_plan, delegated, _plan, (leaf,) = _delegated(db, "Write the tests")
        submit_review_evidence(leaf.id, db=db, evidence="Proof", actor_id="worker")
        db.commit()

        assert delegation.harvest_ready_children(db=db, drives=[], changed_plan_ids=[]) == [delegated.id]
        assert service.get_node(delegated.id, db=db).state == "review_requested"
    finally:
        db.close()
