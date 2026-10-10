"""The default plan read is a compact, bounded, truthful status, never the whole plan.

The budgets: at most 4 Current and 2 Recent rows per page, each section under
its own serialized-byte cap, pagination included. Every listed row
keeps its exact identity and a direct detail read; omitted rows are counted,
never enumerated; a continuation is bound to one snapshot and one clock; the
whole plan is an explicit opt-in. Times are three things, named as such, and a
Turn that cannot be measured is unknown, never zero.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from infra.schema import SCHEMA_SQL
from plans import admission, service, status, status_cursor
from plans.boundary import PlanActor
from plans.current import read_current_plan, read_current_status
from tests.plan_review_helpers import submit_review_evidence

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
CLOCK = NOW.isoformat()
WORKER = PlanActor("worker", "user-1", "project-1")
LONG = "A long enough title to exercise the byte bounds"  # 47 chars, under the 60 limit
BODY = "A description body that the status read must never carry. " * 20


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) "
        "VALUES ('project-1','Project','t','t')"
    )
    db.execute(
        """INSERT INTO threads (id,user_id,project_id,model,created_at,updated_at)
           VALUES ('worker','user-1','project-1','test/model','t','t')"""
    )
    db.commit()
    return db


def _task(db, plan_id: str, title: str, *, state: str = "pending", parent_id=None):
    node = service.add_node(
        plan_id, kind="task", title=title, acceptance_criteria=f"{title} is done",
        description=BODY,
        parent_id=parent_id, db=db,
    )
    if state in ("active", "review_requested", "accepted", "failed"):
        service.transition_node(node.id, "active", actor_id="worker", db=db)
    if state in ("review_requested", "accepted"):
        submit_review_evidence(node.id, db=db, evidence="Proof", actor_id="worker")
    if state == "accepted":
        service.transition_node(
            node.id, "accepted", actor_id="worker", verification_result="passed",
            verified_by="worker", db=db,
        )
    if state == "failed":
        service.transition_node(node.id, "failed", actor_id="worker", db=db)
    db.commit()
    return node


def _turn(db, node_id: str, *, started: str, ended: str | None, status_: str, turn_id: str):
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,ended_at,boot_epoch,
            plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role)
           VALUES (?,'worker',0,'test/model',?,?,?,'boot',
                   (SELECT plan_id FROM plan_nodes WHERE id=?),?,'attempt-'||?,?,'execution')""",
        (turn_id, status_, started, ended, node_id, node_id, turn_id, node_id),
    )
    db.commit()


def _plan(db):
    """A plan wide enough to overflow both sections, with every condition present."""
    plan = service.ensure_plan("thread", "worker", db=db)
    phase = service.add_node(
        plan.id, kind="phase", title="Phase one", acceptance_criteria="All done", db=db,
    )
    done = [_task(db, plan.id, f"{LONG} done {i}", state="accepted") for i in range(3)]
    failed = _task(db, plan.id, f"{LONG} failed", state="failed")
    running = _task(db, plan.id, "Running task", state="active", parent_id=phase.id)
    admission.select(running.id, actor_id="worker", provenance="test", db=db, touch=False)
    waiting = _task(db, plan.id, "Waits on running")
    service.add_dependency(waiting.id, running.id, db=db)
    blocked = _task(db, plan.id, "Blocked by the failure")
    service.add_dependency(blocked.id, failed.id, db=db)
    pending = [_task(db, plan.id, f"{LONG} pending {i}") for i in range(4)]
    db.commit()
    return plan, {
        "done": done, "failed": failed, "running": running, "waiting": waiting,
        "blocked": blocked, "pending": pending, "phase": phase,
    }


def _read(db, **kwargs):
    return read_current_status(actor=WORKER, db=db, **kwargs)


def _bytes(payload) -> int:
    return len(json.dumps(payload, sort_keys=True).encode())


def test_the_default_read_fits_the_budget_and_names_what_it_omits():
    db = _db()
    try:
        plan, nodes = _plan(db)

        result = status.read(db, service._plan_row(db, plan.id), actor=WORKER, clock=CLOCK)

        current, recent = result["current"], result["recent"]
        assert sum(_bytes(row) for row in current["rows"]) <= status.CURRENT_BYTES
        assert sum(_bytes(row) for row in recent["rows"]) <= status.RECENT_BYTES
        assert len(current["rows"]) <= status.CURRENT_ROWS
        assert len(recent["rows"]) <= status.RECENT_ROWS
        assert current["total_count"] == 7  # running, waiting, blocked, 4 pending
        assert current["omitted_count"] == current["total_count"] - len(current["rows"])
        assert recent["total_count"] == 4  # 3 accepted + 1 failed
        assert recent["omitted_count"] == recent["total_count"] - len(recent["rows"])
        assert current["continue_read"].startswith("plan_read(section='current', cursor='")
        assert all(row["detail_read"] == f"plan_read(node_id='{row['id']}')" for row in current["rows"])
        assert all("description" not in row and "acceptance_criteria" not in row for row in current["rows"])
        # Attention first: the real block leads, the running task follows.
        assert [row["id"] for row in current["rows"][:2]] == [nodes["blocked"].id, nodes["running"].id]
        assert current["rows"][0]["condition"] == "blocked"
    finally:
        db.close()


def test_a_dependency_on_open_work_waits_and_a_failed_prerequisite_blocks():
    db = _db()
    try:
        plan, nodes = _plan(db)
        result = status.read(db, service._plan_row(db, plan.id), actor=WORKER, clock=CLOCK)
        rows = {row["id"]: row for row in result["current"]["rows"]}

        assert rows[nodes["blocked"].id]["condition"] == "blocked"
        assert result["severity"] == "blocked"
        # Continue until the waiting row is listed.
        waiting = _find(db, plan.id, nodes["waiting"].id, "current")
        assert waiting["condition"] == "waiting"
        assert waiting["reason"] == "After Running task"
    finally:
        db.close()


def _find(db, plan_id: str, node_id: str, section: str) -> dict:
    row_ = service._plan_row(db, plan_id)
    page = status.read(db, row_, actor=WORKER, clock=CLOCK)[section]
    while True:
        for row in page["rows"]:
            if row["id"] == node_id:
                return row
        assert "cursor" in page, "row never listed"
        page = status.read(db, row_, actor=WORKER, section=section, cursor=page["cursor"])


def test_continuation_walks_every_row_once_at_the_first_pages_clock():
    db = _db()
    try:
        plan, nodes = _plan(db)
        row_ = service._plan_row(db, plan.id)
        first = status.read(db, row_, actor=WORKER, clock=CLOCK)["current"]
        seen = [row["id"] for row in first["rows"]]
        page = first
        while "cursor" in page:
            page = status.read(db, row_, actor=WORKER, section="current", cursor=page["cursor"])
            assert page["as_of"] == CLOCK
            seen.extend(row["id"] for row in page["rows"])
        assert len(seen) == len(set(seen)) == first["total_count"]
        assert page["omitted_count"] == 0

        # The plan moves: the cursor refuses and names the next action.
        service.transition_node(nodes["pending"][0].id, "active", actor_id="worker", db=db)
        db.commit()
        with pytest.raises(service.PlanError, match="plan_read\\(\\) again"):
            status.read(db, row_, actor=WORKER, section="current", cursor=first["cursor"])
    finally:
        db.close()


def test_a_first_row_too_large_for_its_section_keeps_identity_and_says_so():
    db = _db()
    try:
        plan = service.ensure_plan("thread", "worker", db=db)
        node = _task(db, plan.id, "任务" * 30, state="active")  # 60 chars, 180 bytes
        row_ = service._plan_row(db, plan.id)
        tiny = status.read(db, row_, actor=WORKER, clock=CLOCK)["current"]["rows"][0]
        assert tiny["id"] == node.id and tiny["truncated"] is True
        assert tiny["detail_read"] == f"plan_read(node_id='{node.id}')"
    finally:
        db.close()


def test_times_are_three_things_and_an_unmeasurable_turn_is_unknown_not_zero():
    db = _db()
    try:
        plan, nodes = _plan(db)
        running = nodes["running"].id
        _turn(db, running, started=(NOW - timedelta(minutes=10)).isoformat(),
              ended=(NOW - timedelta(minutes=9)).isoformat(), status_="completed", turn_id="t1")
        _turn(db, running, started=(NOW - timedelta(minutes=8)).isoformat(),
              ended=(NOW - timedelta(minutes=9)).isoformat(), status_="error", turn_id="t2")
        _turn(db, running, started=(NOW - timedelta(seconds=30)).isoformat(),
              ended=None, status_="running", turn_id="t3")
        row = _find(db, plan.id, running, "current")

        assert row["execution_time"] == {
            "seconds": 90, "unknown_turns": 1,
            "turns": {"completed": 1, "error": 1, "cancelled": 0, "running": 1, "waiting_retry": 0},
        }
        assert set(row["ages"]) == {"created_seconds", "in_state_seconds", "waited_seconds"}
        plan_row = service._plan_row(db, plan.id)
        # `running` is a child of `phase`; the commitment total is true only
        # at the commitment's one root, so it is null here and present there.
        detail = status.node_detail(db, plan_row, running, actor=WORKER, clock=CLOCK)
        assert detail["commitment_cost"] is None
        root_detail = status.node_detail(db, plan_row, nodes["phase"].id, actor=WORKER, clock=CLOCK)
        assert "elapsed_seconds" in root_detail["commitment_cost"]
    finally:
        db.close()


def test_the_current_and_waiting_rows_carry_their_own_waited_seconds():
    """W6: waiting time sits beside the ages on running and waiting rows only."""
    db = _db()
    try:
        plan, nodes = _plan(db)
        later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        row_ = service._plan_row(db, plan.id)
        pages = []
        page = status.read(db, row_, actor=WORKER, clock=later)["current"]
        pages.append(page)
        while "cursor" in page:
            page = status.read(db, row_, actor=WORKER, section="current", cursor=page["cursor"])
            pages.append(page)
        rows = {row["id"]: row for page in pages for row in page["rows"]}

        waited = rows[nodes["waiting"].id]["ages"]["waited_seconds"]
        # The dependency on the running task has held since it was added.
        assert 3590 <= waited <= 3610
        assert rows[nodes["running"].id]["ages"]["waited_seconds"] == 0
        # Blocked on a failed prerequisite: the unmet dependency is counted, so shown.
        assert rows[nodes["blocked"].id]["condition"] == "blocked"
        assert 3590 <= rows[nodes["blocked"].id]["ages"]["waited_seconds"] <= 3610
        assert "waited_seconds" not in rows[nodes["pending"][0].id]["ages"]
        assert all(
            sum(_bytes(row) for row in page["rows"]) <= status.CURRENT_BYTES for page in pages
        )
    finally:
        db.close()


def test_a_failed_recent_row_keeps_its_execution_facts():
    db = _db()
    try:
        plan, nodes = _plan(db)
        failed = nodes["failed"].id
        _turn(db, failed, started=(NOW - timedelta(minutes=5)).isoformat(),
              ended=(NOW - timedelta(minutes=4)).isoformat(), status_="cancelled", turn_id="t9")
        row = _find(db, plan.id, failed, "recent")
        assert row["state"] == "failed"
        assert row["execution_time"]["seconds"] == 60
        assert row["execution_time"]["turns"]["cancelled"] == 1
        assert row["settled_at"]
    finally:
        db.close()


def test_the_overview_names_the_current_task_or_that_there_is_none():
    db = _db()
    try:
        plan, nodes = _plan(db)
        row_ = service._plan_row(db, plan.id)
        result = status.read(db, row_, actor=WORKER, clock=CLOCK)
        assert result["current_task"]["id"] == nodes["running"].id
        assert result["no_current_task"] is False
        assert result["plan"] == {"id": plan.id, "revision": row_["revision"], "state": "active"}
        # The four finished top-level steps fold out; the phase and open work remain.
        assert result["progress"]["total"] == 7 and result["progress"]["done"] == 0

        admission.release(nodes["running"].id, "accepted", db=db)
        db.commit()
        result = status.read(db, row_, actor=WORKER, clock=CLOCK)
        assert result["current_task"] is None and result["no_current_task"] is True
        assert result["next_step"] is not None
    finally:
        db.close()


def test_node_detail_is_exact_and_a_stranger_is_refused_by_name():
    db = _db()
    try:
        plan, nodes = _plan(db)
        row_ = service._plan_row(db, plan.id)
        detail = status.node_detail(db, row_, nodes["running"].id, actor=WORKER, clock=CLOCK)
        assert detail["description"].startswith("A description body")
        assert detail["acceptance_criteria"] == "Running task is done"
        assert detail["revision"] >= nodes["running"].revision
        assert detail["events"] and detail["events"][0]["created_at"]
        assert detail["created_at"]
        assert "execution" in detail and "commitment_cost" in detail
        # Every durable column of the node, not only the presentation.
        for column in (
            "plan_id", "order_key", "assignee", "spawn_kind", "owner_thread_id",
            "executor_thread_id", "spawned_thread_id", "follow_up_of",
            "checkpoint_exception", "watchdog_budget_seconds", "updated_at",
        ):
            assert column in detail, column
        assert detail["plan_id"] == plan.id
        with pytest.raises(service.PlanError, match="not in this plan"):
            status.node_detail(db, row_, "nope", actor=WORKER)
    finally:
        db.close()


def test_the_tool_read_defaults_to_status_and_the_whole_plan_is_opt_in():
    db = _db()
    try:
        _plan(db)
        compact = _read(db)
        full = read_current_plan(actor=WORKER, db=db)
        assert "current" in compact and "nodes" not in compact
        assert len(full["nodes"]) == 8  # the four folded steps are counted, not listed
        listed = len(compact["current"]["rows"]) + len(compact["recent"]["rows"])
        assert listed <= status.CURRENT_ROWS + status.RECENT_ROWS < len(full["nodes"])
        # Neither read carries a node body; plan_read(node_id=...) does.
        assert BODY not in json.dumps(compact) and BODY not in json.dumps(full)
    finally:
        db.close()


def test_a_cursor_without_its_section_or_from_another_plan_is_refused():
    db = _db()
    try:
        plan, _nodes = _plan(db)
        row_ = service._plan_row(db, plan.id)
        cursor = status.read(db, row_, actor=WORKER, clock=CLOCK)["current"]["cursor"]
        with pytest.raises(service.PlanError, match="pass section"):
            status.read(db, row_, actor=WORKER, cursor=cursor)
        with pytest.raises(service.PlanError, match="stale"):
            status.read(db, row_, actor=WORKER, section="recent", cursor=cursor)
        with pytest.raises(service.PlanError, match="stale"):
            status.read(db, row_, actor=WORKER, section="current", cursor="not-a-cursor")
        backwards = status_cursor.decode(cursor, plan_id=plan.id, section="current")
        backwards["o"] = -1
        with pytest.raises(service.PlanError, match="stale"):
            status.read(
                db, row_, actor=WORKER, section="current",
                cursor=status_cursor.encode(backwards),
            )
    finally:
        db.close()


def test_current_ranks_live_work_before_a_sequenced_wait():
    """A step waiting only on moving work reads Waiting and ranks after it (Gate 3)."""
    db = _db()
    try:
        plan, nodes = _plan(db)
        rows = status.read(db, service._plan_row(db, plan.id), actor=WORKER, clock=CLOCK)
        ids = [row["id"] for row in rows["current"]["rows"]]
        assert ids.index(nodes["running"].id) < ids.index(nodes["waiting"].id)
        waiting = rows["current"]["rows"][ids.index(nodes["waiting"].id)]
        assert waiting["label"] == "waiting"
    finally:
        db.close()
