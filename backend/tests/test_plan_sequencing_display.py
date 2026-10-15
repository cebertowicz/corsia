"""Ordinary sequencing reads as waiting; only work that cannot proceed reads blocked.

Owner decision, 2026-09-16: a task whose only causes are open dependencies can still
start once its prerequisites finish, so every surface shows it as a neutral
wait named "After <prerequisite>". A hold an agent or an external party owns
reads as a wait too (owner decision, 2026-09-24). A failed prerequisite, a
user-owned hold, or any derived cause still shows blocked. That includes the plan liveness the
subagent dot and the digest read. Canonical admission is unchanged: the
condition stays ``blocked``, and the actionable work that wakes and selects
is the same. The pane also carries the soft oversize signal the digest carries.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3

import pytest

from infra.schema import SCHEMA_SQL
from plans import (
    admission, holds, mutations, not_moving, operator_node_state, operator_projection, projections,
    service, status, status_context,
)
from plans.boundary import PlanActor

HUMAN = PlanActor(None, "user", None, kind="user")
OWNER = PlanActor("owner", "user", "project")
PROVENANCE = "test.plan.sequencing_display"
WAITING = {"label_key": "waiting", "tone_key": "waiting"}
BLOCKED = WAITING  # Internal blocking stays exact; the four-state surface says Waiting.


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project','Project','t','t')"
    )
    for thread_id, title, spawned_by in (
        ("owner", "Plan owner", None), ("child", "Delegated worker", "owner"),
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


def _read(db) -> tuple[dict, dict[str, dict]]:
    entry = operator_projection.read_thread("owner", actor=HUMAN, db=db)["plans"][0]
    return entry, {node["id"]: node for node in entry["nodes"]}


def _status_row(db, plan_id: str, node_id: str) -> dict:
    result = status.read(db, service._plan_row(db, plan_id), actor=OWNER)
    return next(row for row in result["current"]["rows"] if row["id"] == node_id)


def test_a_dependency_on_open_work_displays_waiting_after_its_prerequisite():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        build = _task(plan.id, "Build the API", db=db)
        ship = _task(plan.id, "Ship the client", db=db)
        service.add_dependency(ship.id, build.id, db=db)

        entry, nodes = _read(db)
        shipped = nodes[ship.id]
        # Canonical admission is untouched: gating still reads blocked, and the
        # work a wake or selection acts on is still only the prerequisite.
        assert admission.project(ship.id, db=db)["condition"] == "blocked"
        assert shipped["execution"]["condition"] == "blocked"
        assert projections.liveness_signal("owner", db=db).node_ids == (build.id,)
        # The display owner reads ordinary order as a neutral wait.
        assert entry["progress"]["liveness"] == "waiting"
        assert shipped["display"] == WAITING
        assert shipped["status_reason"] == "After Build the API"
        assert entry["display"]["label_key"] != "blocked"
        assert entry["progress"]["distribution"]["blocked"] == 0
        assert entry["progress"]["distribution"]["waiting"] == 1

        row = _status_row(db, plan.id, ship.id)
        assert row["condition"] == "waiting"
        assert row["label"] == "waiting"
        assert row["reason"] == "After Build the API"
    finally:
        db.close()


def test_several_prerequisites_name_the_first_and_count_the_rest():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        first = _task(plan.id, "Design", db=db)
        second = _task(plan.id, "Review", db=db)
        ship = _task(plan.id, "Ship", db=db)
        service.add_dependency(ship.id, first.id, db=db)
        service.add_dependency(ship.id, second.id, db=db)

        _entry, nodes = _read(db)
        assert nodes[ship.id]["display"] == WAITING
        assert nodes[ship.id]["status_reason"] == "After Design and 1 more"
    finally:
        db.close()


def test_a_failed_prerequisite_still_blocks():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        prepare = _task(plan.id, "Prepare", db=db)
        ship = _task(plan.id, "Ship", db=db)
        service.add_dependency(ship.id, prepare.id, db=db)
        mutations.fail_node(prepare.id, actor_id="owner", provenance=PROVENANCE, db=db)

        entry, nodes = _read(db)
        assert nodes[ship.id]["display"] == BLOCKED
        assert nodes[ship.id]["status_reason"] == (
            "Prepare failed and cannot satisfy this dependency"
        )
        assert entry["display"]["label_key"] == "waiting"
        assert _status_row(db, plan.id, ship.id)["label"] == "waiting"
    finally:
        db.close()


def test_a_hold_beside_a_dependency_still_blocks():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        build = _task(plan.id, "Build", db=db)
        ship = _task(plan.id, "Ship", db=db)
        service.add_dependency(ship.id, build.id, db=db)
        with service._write(db):
            holds.block(
                ship.id, reason_key="credentials_missing", reason="Needs a deploy key",
                owner_kind="user", owner_id=None,
                expected_revision=service._node_row(db, ship.id)["revision"],
                actor_id="owner", provenance=PROVENANCE, db=db,
            )

        entry, nodes = _read(db)
        assert nodes[ship.id]["display"] == BLOCKED
        assert nodes[ship.id]["status_reason"] == "Needs a deploy key"
        # Nothing moves: the ready row waits its turn, so the header reads Waiting.
        assert entry["display"]["label_key"] == "waiting"
        assert status.read(
            db, service._plan_row(db, plan.id), actor=OWNER,
        )["severity"] == "ready"
    finally:
        db.close()


@pytest.mark.parametrize("owner_kind", ["thread", "external", "system"])
def test_a_blocker_nobody_asks_the_user_to_clear_reads_as_a_wait(owner_kind):
    """Red is for failed work and what the user must clear (owner decision, 2026-09-24).

    Such a blocker is a wait with a deadline (stuck plans, option C), and its
    row names what it waits on and until when.
    """
    from plans import blocked_wait, watch_operations

    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        ship = _task(plan.id, "Ship", db=db)
        with service._write(db):
            params = blocked_wait.watch_params({
                "node_id": ship.id, "reason_key": "awaiting_production_deploy",
                "reason": "production to serve the fix", "owner_kind": owner_kind,
                "expected_revision": service._node_row(db, ship.id)["revision"],
            })
            watch = watch_operations.arm_write(plan.id, params, OWNER, db)
        until = datetime.fromisoformat(watch.condition["value"]).astimezone(timezone.utc)
        # The deadline leads, so a row cut to its byte budget keeps it.
        reason = f"Until {until:%Y-%m-%dT%H:%MZ} · production to serve the fix"

        entry, nodes = _read(db)
        assert nodes[ship.id]["display"] == WAITING
        assert nodes[ship.id]["status_reason"] == reason
        # Nothing else can move, so the header names the wait's end (R9).
        assert entry["display"] == {
            **WAITING,
            "not_moving": {
                "need_you": 0, "next_wait_ends_at": watch.condition["value"],
                "next_overdue_at": (
                    datetime.fromisoformat(watch.condition["value"]) + not_moving.OVERDUE_GRACE
                ).isoformat(),
                "overdue_waits": 0, "paused_by_you": 0, "set_aside": 0,
            },
        }
        assert entry["progress"]["liveness"] == "waiting"
        read = status.read(db, service._plan_row(db, plan.id), actor=OWNER)
        assert read["severity"] == "not_moving"
        row = next(row for row in read["current"]["rows"] if row["id"] == ship.id)
        assert (row["condition"], row["reason"]) == ("waiting", reason)
    finally:
        db.close()


def test_a_blocker_the_user_clears_reads_red():
    """The one blocker that stays a hold is the user's, and it reads blocked."""
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        ship = _task(plan.id, "Ship", db=db)
        with service._write(db):
            holds.block(
                ship.id, reason_key="needs_approval", reason="Needs the user's approval",
                owner_kind="user", owner_id=None,
                expected_revision=service._node_row(db, ship.id)["revision"],
                actor_id="owner", provenance=PROVENANCE, db=db,
            )

        entry, nodes = _read(db)
        assert nodes[ship.id]["execution"]["condition"] == "blocked"
        assert nodes[ship.id]["display"] == WAITING
        assert entry["progress"]["liveness"] == "blocked"
    finally:
        db.close()


def test_a_delegated_child_that_can_never_finish_stays_blocked():
    """A derived cause is no placed hold: its thread owner does not make it quiet."""
    from plans.wait_causes import DELEGATED_CHILD_UNAVAILABLE

    dead_child = {
        "kind": "blocked", "reason_key": DELEGATED_CHILD_UNAVAILABLE,
        "reason": "The delegated child cannot finish this step: archived",
        "owner_kind": "thread", "owner_id": "owner", "child_thread_id": "child",
    }
    execution = {"condition": "blocked", "causes": [dead_child]}
    assert not operator_node_state.quiet(execution, set())


def test_the_digest_severity_is_not_blocked_for_pure_sequencing():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        build = _task(plan.id, "Build", db=db)
        ship = _task(plan.id, "Ship", db=db)
        service.add_dependency(ship.id, build.id, db=db)

        thin = status_context.projection(db, service._plan_row(db, plan.id), actor=OWNER)
        # A neutral wait beside ready work, never a block.
        assert thin["severity"] == "ready"
        assert thin["progress"]["liveness"] == "waiting"
        assert "blocked" not in json.dumps(
            {key: thin[key] for key in ("severity", "progress", "current_task", "next_step")}
        )
        assert service.progress(plan.id, db=db).liveness == "waiting"
    finally:
        db.close()


def test_the_digest_liveness_stays_blocked_for_a_failed_prerequisite():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        prepare = _task(plan.id, "Prepare", db=db)
        build = _task(plan.id, "Build", db=db)
        ship = _task(plan.id, "Ship", db=db)
        service.add_dependency(ship.id, prepare.id, db=db)
        service.add_dependency(build.id, ship.id, db=db)
        mutations.fail_node(prepare.id, actor_id="owner", provenance=PROVENANCE, db=db)

        thin = status_context.projection(db, service._plan_row(db, plan.id), actor=OWNER)
        # One real block outranks the sequenced step behind it.
        assert thin["progress"]["liveness"] == "blocked"
        assert thin["severity"] == "blocked"
    finally:
        db.close()


def _delegated_child_plan(db):
    """Attach a child plan to a parent step; return the parent step and child plan."""
    parent_plan = service.ensure_plan("thread", "owner", db=db)
    step = _task(parent_plan.id, "Delegated build", spawn_kind="claude-code", db=db)
    service.transition_node(step.id, "active", db=db)
    child_plan = service.ensure_plan("thread", "child", db=db)
    mutations.attach_spawned_thread(step.id, "child", db=db)
    db.commit()
    return step, child_plan


def test_the_subagent_dot_reads_waiting_for_a_child_plan_that_is_only_sequenced():
    db = _db()
    try:
        step, child_plan = _delegated_child_plan(db)
        build = _task(child_plan.id, "Build", db=db)
        ship = _task(child_plan.id, "Ship", db=db)
        service.add_dependency(ship.id, build.id, db=db)

        _entry, nodes = _read(db)
        assert nodes[step.id]["child"]["progress"]["liveness"] == "waiting"
        assert projections.liveness_signal("child", db=db).node_ids == (build.id,)
    finally:
        db.close()


def test_the_subagent_dot_reads_blocked_for_a_child_plan_with_a_real_block():
    db = _db()
    try:
        step, child_plan = _delegated_child_plan(db)
        build = _task(child_plan.id, "Build", db=db)
        ship = _task(child_plan.id, "Ship", db=db)
        service.add_dependency(ship.id, build.id, db=db)
        mutations.fail_node(build.id, actor_id="child", provenance=PROVENANCE, db=db)

        _entry, nodes = _read(db)
        assert nodes[step.id]["child"]["progress"]["liveness"] == "blocked"
    finally:
        db.close()


def test_a_queued_row_says_not_started_and_a_running_row_names_none():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        ready = _task(plan.id, "Ready", db=db)
        running = _task(plan.id, "Running", db=db)
        with service._write(db):
            admission.activate(
                running.id, expected_revision=running.revision, actor_id="owner",
                provenance=PROVENANCE, db=db,
            )
        admission.select(running.id, actor_id="owner", provenance=PROVENANCE, db=db, touch=False)

        _entry, nodes = _read(db)
        # Only moving work reads Working; queued ready work says why it is still.
        assert nodes[ready.id]["display"] == {"label_key": "waiting", "tone_key": "waiting"}
        assert nodes[ready.id]["status_reason"] == "Not started"
        assert nodes[running.id]["display"] == {"label_key": "working", "tone_key": "active"}
        assert nodes[running.id]["status_reason"] is None
    finally:
        db.close()


def test_the_pane_carries_the_oversize_signal_only_above_the_budget():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "owner", db=db)
        budget = status_context.OPEN_NODE_BUDGET
        for index in range(budget):
            _task(plan.id, f"Step {index}", db=db)

        entry, _nodes = _read(db)
        assert entry["oversize"] is None
        plan_row = service._plan_row(db, plan.id)
        assert "oversize" not in status_context.projection(db, plan_row, actor=OWNER)

        _task(plan.id, "One too many", db=db)
        entry, _nodes = _read(db)
        expected = {"open": budget + 1, "budget": budget}
        assert entry["oversize"] == expected
        plan_row = service._plan_row(db, plan.id)
        assert status_context.projection(db, plan_row, actor=OWNER)["oversize"] == expected
    finally:
        db.close()
