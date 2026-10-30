"""A delegated step reads In Progress while its child plan is working.

A step in the parent's plan can stand for a child thread's own attached plan.
The parent is not running that step itself, so its execution condition stays
what admission says (`ready` here) and no slot is taken. But the child's plan
is doing the work, so the plan pane must not show the step as idle: its display
reads `in_progress` and it is current work while a step in the attached child
plan runs, and the parent plan's rollup (liveness, leaf distribution and
overview state) agrees with the row. The step's own causes still win: a paused,
blocked or review-requested parent step says so even while the child works,
and a child plan with nothing running leaves the step's usual label (a ready
step in a free slot reads `next`). Motion is not masked by attention: a child
plan that reads needs-review because one step awaits review still moves the
parent while another of its steps runs. A step delegated again below the child
reads in progress at every level above the running work. "Runs" means its
worker's Turn is running: a selected step whose worker has no Turn and no
queued input is between turns, and the step waits on it instead.
"""

from __future__ import annotations

import sqlite3

import pytest

from infra.schema import SCHEMA_SQL
from plans import admission, holds, mutations, operator_projection, projections, rollup, service
from plans.boundary import PlanActor
from tests.plan_blocked_wait_helpers import wait_on
from tests.plan_review_helpers import submit_review_evidence
from tests.plan_writer_authorizer import raw_plan_seed

HUMAN = PlanActor(None, "user", None, kind="user")
WORKING = {"label_key": "working", "tone_key": "active"}


def _db() -> sqlite3.Connection:
    """A project with a parent thread, its direct child, and a grandchild."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project','Project','t','t')"
    )
    for thread_id, title, spawned_by in (
        ("owner", "Plan owner", None),
        ("child", "Delegated worker", "owner"),
        ("grandchild", "Nested worker", "child"),
    ):
        db.execute(
            """INSERT INTO threads
               (id,user_id,project_id,title,spawned_by,created_at,updated_at)
               VALUES (?,'user','project',?,?,'t','t')""",
            (thread_id, title, spawned_by),
        )
    db.commit()
    return db


def _task(plan_id: str, title: str, *, db, **kwargs):
    return service.add_node(
        plan_id, kind="task", title=title,
        acceptance_criteria=f"{title} is proven", db=db, **kwargs,
    )


def _run(db, node_id: str, actor_id: str) -> None:
    """Select a step for its worker, as activate does, while the worker's Turn runs."""
    row = service._node_row(db, node_id)
    admission.activate(
        node_id, expected_revision=row["revision"],
        actor_id=actor_id, provenance="test.delegated_display", db=db,
    )
    db.execute(
        """INSERT OR IGNORE INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES (?,?,0,'native/model','running','2000-01-01T00:00:00','boot-1')""",
        (f"turn-{actor_id}", actor_id),
    )


def _delegated_step(db, *, child_running: bool):
    """Return the parent's delegated step and the child's step below it."""
    parent_plan = service.ensure_plan("thread", "owner", db=db)
    step = _task(parent_plan.id, "Delegated research", spawn_kind="claude-code", db=db)
    service.transition_node(step.id, "active", db=db)
    child_plan = service.ensure_plan("thread", "child", db=db)
    child_step = _task(child_plan.id, "Gather sources", db=db)
    mutations.attach_spawned_thread(step.id, "child", db=db)
    if child_running:
        _run(db, child_step.id, "child")
    db.commit()
    return step, child_step


def _plan(db, thread_id: str) -> dict:
    payload = operator_projection.read_thread(thread_id, actor=HUMAN, db=db)
    return payload["plans"][0]


def _node(db, thread_id: str, node_id: str) -> dict:
    nodes = {node["id"]: node for node in _plan(db, thread_id)["nodes"]}
    return nodes[node_id]


def test_a_delegated_step_and_its_plan_read_in_progress_while_the_child_plan_runs():
    db = _db()
    try:
        step, _child_step = _delegated_step(db, child_running=True)

        node = _node(db, "owner", step.id)
        entry = _plan(db, "owner")

        assert node["execution"]["condition"] == "waiting"
        assert node["execution"]["causes"][0]["reason_key"] == "delegated_child"
        assert node["child"]["progress"]["liveness"] == "advancing"
        assert node["display"] == WORKING
        assert entry["current_node_ids"] == [step.id]
        assert entry["next_node_ids"] == []
        # The child's leaves stay in the child's plan: the parent counts one step.
        assert (entry["progress"]["done"], entry["progress"]["total"]) == (0, 1)
        assert entry["progress"]["liveness"] == "advancing"
        assert entry["progress"]["distribution"]["active"] == 1
        assert entry["progress"]["distribution"]["pending_ready"] == 0
        assert entry["display"] == WORKING
    finally:
        db.close()


def test_a_child_plan_awaiting_review_still_moves_the_parent_while_another_step_runs():
    db = _db()
    try:
        step, child_step = _delegated_step(db, child_running=False)
        child_plan = service.ensure_plan("thread", "child", db=db)
        summary = _task(child_plan.id, "Summarize sources", db=db)
        _run(db, summary.id, "child")
        submit_review_evidence(summary.id, db=db, actor_id="child")
        # As the plan tool's request_review does, the reviewed step frees its slot.
        admission.release(summary.id, "review_requested", db=db)
        _run(db, child_step.id, "child")
        db.commit()

        node = _node(db, "owner", step.id)
        entry = _plan(db, "owner")

        # The chip keeps the child's attention state; the row keeps its motion.
        assert node["child"]["progress"]["liveness"] == "needs-review"
        assert node["display"] == WORKING
        assert entry["next_node_ids"] == []
        assert entry["display"] == WORKING
    finally:
        db.close()


def test_a_paused_delegated_step_still_says_paused_while_its_child_runs():
    db = _db()
    try:
        step, _child_step = _delegated_step(db, child_running=True)
        row = service._node_row(db, step.id)
        holds.pause(
            step.id, "Waiting on the owner", expected_revision=row["revision"],
            actor_id="owner", provenance="test.delegated_display", db=db,
        )
        db.commit()

        node = _node(db, "owner", step.id)
        entry = _plan(db, "owner")

        assert node["child"]["progress"]["liveness"] == "advancing"
        assert node["display"] == {"label_key": "waiting", "tone_key": "waiting"}
        assert entry["progress"]["liveness"] == "paused"
        assert entry["display"] == {"label_key": "waiting", "tone_key": "waiting"}
    finally:
        db.close()


def test_a_delegated_step_its_own_agent_holds_reads_as_a_neutral_wait():
    db = _db()
    try:
        step, _child_step = _delegated_step(db, child_running=True)
        wait_on(
            db, step.id, reason="the launch brief", owner_kind="thread",
            reason_key="missing_instructions",
        )
        db.commit()

        node = _node(db, "owner", step.id)
        entry = _plan(db, "owner")

        assert node["child"]["progress"]["liveness"] == "advancing"
        # Only the user's blocker reads red (#2213): the agent's own is a wait.
        assert node["display"] == {"label_key": "waiting", "tone_key": "waiting"}
        assert entry["progress"]["liveness"] == "waiting"
        assert entry["display"]["label_key"] == "waiting"
        assert entry["display"]["tone_key"] == "waiting"
        # EW3: the plan says what it waits for, in the reason's own words.
        assert entry["display"]["wait"]["label"] == "the launch brief"
    finally:
        db.close()


def test_a_waiting_child_tells_its_parent_what_it_waits_for():
    """EW4: the parent's child summary carries the child's wait; its state stays liveness."""
    db = _db()
    try:
        step, child_step = _delegated_step(db, child_running=False)
        wait_on(
            db, child_step.id, reason="Waiting for the vendor reply", owner_kind="external",
            reason_key="vendor_reply", actor=PlanActor("child", "user", "project"),
        )
        db.commit()

        child = _node(db, "owner", step.id)["child"]

        assert child["progress"]["liveness"] == "waiting"
        assert child["wait"]["label"] == "the vendor reply"
        assert child["wait"]["more"] == 0
    finally:
        db.close()


def test_a_delegated_step_whose_child_is_between_turns_waits_on_it():
    """D4 (release 2): the child still has open steps, so the step is its to move.

    Until slice 4 this step read ready and was the owner's next task, which let
    liveness wake the owner while the child had work in hand. The child is not
    running either, so the step waits without reading as progress.
    """
    db = _db()
    try:
        step, _child_step = _delegated_step(db, child_running=False)

        node = _node(db, "owner", step.id)
        entry = _plan(db, "owner")

        assert node["execution"]["condition"] == "waiting"
        assert node["child"]["progress"]["liveness"] != "advancing"
        assert node["display"] == {"label_key": "waiting", "tone_key": "waiting"}
        assert entry["current_node_ids"] == [step.id]
        assert entry["progress"]["liveness"] == "waiting"
    finally:
        db.close()


def test_a_delegated_step_awaiting_review_reads_as_review_once_the_child_is_done():
    db = _db()
    try:
        step, child_step = _delegated_step(db, child_running=True)
        submit_review_evidence(child_step.id, db=db, actor_id="child")
        service.transition_node(
            child_step.id, "accepted", verification_result="passed",
            verified_by="child", db=db,
        )
        submit_review_evidence(step.id, db=db, actor_id="owner")
        db.commit()

        node = _node(db, "owner", step.id)
        entry = _plan(db, "owner")

        # The child's finished step folded out of its counter.
        assert node["child"]["progress"] == {"done": 0, "total": 0, "open": 0, "liveness": "idle"}
        assert node["execution"]["condition"] == "review"
        assert node["display"] == {"label_key": "working", "tone_key": "active"}
        assert entry["progress"]["liveness"] == "needs-review"
        assert entry["display"] == {"label_key": "working", "tone_key": "active"}
    finally:
        db.close()


def test_a_step_delegated_again_reads_in_progress_at_every_level_above_the_work():
    db = _db()
    try:
        owner_plan = service.ensure_plan("thread", "owner", db=db)
        step = _task(owner_plan.id, "Delegated research", spawn_kind="claude-code", db=db)
        service.transition_node(step.id, "active", db=db)
        child_plan = service.ensure_plan("thread", "child", db=db)
        child_step = _task(
            child_plan.id, "Delegated reading", spawn_kind="claude-code", db=db,
        )
        service.transition_node(child_step.id, "active", db=db)
        mutations.attach_spawned_thread(step.id, "child", db=db)
        grandchild_plan = service.ensure_plan("thread", "grandchild", db=db)
        grandchild_step = _task(grandchild_plan.id, "Read one source", db=db)
        mutations.attach_spawned_thread(child_step.id, "grandchild", db=db)
        _run(db, grandchild_step.id, "grandchild")
        db.commit()

        child_row = _node(db, "child", child_step.id)
        owner_row = _node(db, "owner", step.id)
        batched = rollup.rollup_threads(
            [service._plan_row(db, owner_plan.id)], db=db, child_depth=0,
        )[0]

        # The child is not running its step either; only the grandchild runs.
        assert child_row["execution"]["condition"] == "waiting"
        assert child_row["child"]["progress"]["liveness"] == "advancing"
        assert child_row["display"] == WORKING
        assert owner_row["child"]["progress"]["liveness"] == "advancing"
        assert owner_row["display"] == WORKING
        assert _plan(db, "owner")["display"] == WORKING
        assert batched.progress["liveness"] == "advancing"
    finally:
        db.close()


def test_a_cycle_through_attached_child_plans_is_a_hierarchy_error():
    db = _db()
    try:
        _step, child_step = _delegated_step(db, child_running=False)
        owner_plan = service.ensure_plan("thread", "owner", db=db)
        child_plan = service.ensure_plan("thread", "child", db=db)
        # Corrupt hierarchy: the owner's plan hangs below the child's plan, and
        # the child's step is delegated back to the owner.
        db.execute(
            "UPDATE plans SET parent_plan_id=? WHERE id=?", (child_plan.id, owner_plan.id),
        )
        with raw_plan_seed(db):
            db.execute(
                "UPDATE plan_nodes SET spawned_thread_id='owner' WHERE id=?", (child_step.id,),
            )
        db.commit()

        with pytest.raises(service.PlanError, match="cycle"):
            projections.progress(owner_plan.id, db=db)
    finally:
        db.close()
