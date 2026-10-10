"""Who holds an executor slot, and when it is genuinely free.

A plan reserves one slot per (plan, executor) with a unique index, so the
question "is this slot free" is answered in three places that must agree: the
database constraint, the read path that tells an agent what is in its way
(`admission_projection._conflict_index`), and the write path that actually
refuses selection (`admission_attempts.selection_conflict`). The last two
read the one occupancy query, `admission_attempts.slot_holders`, for exactly
that reason.

Disagreement is not cosmetic: it produces a plan surface promising a move the
database then refuses, with no operation able to resolve it.
"""

from __future__ import annotations

import sqlite3

import pytest

from infra.database import get_db
from infra.schema import SCHEMA_SQL
from plans import admission, admission_attempts, service
from plans.boundary import PlanActor, mutate
from tests.event_routing_helpers import mk_session
from tests.plan_execution_helpers import (
    ProbeTool,
    add_task,
    execute_as,
    open_turn,
)
from tests.plan_review_helpers import submit_review_evidence
from tools.registry import ToolRegistry


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    for thread_id, spawned_by in (("owner", None), ("worker", "owner")):
        connection.execute(
            "INSERT INTO threads "
            "(id,user_id,spawned_by,model,created_at,updated_at) "
            "VALUES (?, 'user', ?, 'test/model', 't', 't')",
            (thread_id, spawned_by),
        )
    connection.commit()
    yield connection
    connection.close()


def _registry(tool: ProbeTool) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(tool)
    return registry


def _mutate(db, plan_id, operation, params, key):
    return mutate(
        plan_id, operation, params, actor=PlanActor("owner", "user", None),
        provenance="test.plan_slots", mutation_id=key, db=db,
    ).result


def _task(db, plan_id, title, **kwargs):
    return service.add_node(
        plan_id, kind="task", title=title,
        acceptance_criteria=f"{title} is verified", db=db, **kwargs,
    )


@pytest.mark.asyncio
async def test_user_assigned_work_never_takes_the_agent_executor_slot(isolated_db):
    """A human obligation is not agent work.

    reconcile_plan runs on every settlement and turn terminal, in order_key
    order. Without an assignee filter it hands the agent's single per-plan slot
    to a task only the user can settle: the agent's tool cost is then billed to
    that task, its own ready work is conflict-blocked behind it, and liveness
    -- which does exclude user work -- reports nothing to wake for.
    """
    thread_id = mk_session("user-assigned-slot")
    db = get_db()
    identity = open_turn(db, thread_id)
    plan = service.ensure_plan("thread", thread_id, db=db)
    user_task = service.add_node(
        plan.id, kind="task", title="The owner approves the release",
        acceptance_criteria="approved", assignee="user", order_key=0, db=db,
    )
    agent_task = add_task(db, plan.id, "Agent implements", order_key=1)
    service.transition_node(user_task.id, "active", db=db)
    service.transition_node(agent_task.id, "active", db=db)
    admission.reconcile_plan(
        plan.id, actor_id=None, provenance="turn_terminal", db=db,
    )
    db.commit()

    selected = db.execute(
        """SELECT n.id,n.assignee FROM plan_node_attempts a
           JOIN plan_nodes n ON n.id=a.node_id
           WHERE a.plan_id=? AND a.released_at IS NULL""",
        (plan.id,),
    ).fetchall()

    # The agent's own work takes the slot; the user's task is left alone.
    assert [row["id"] for row in selected] == [agent_task.id]
    assert all(row["assignee"] != "user" for row in selected)

    from plans import projections
    assert projections.liveness_signal(thread_id, db=db).node_ids == (agent_task.id,)

    tool = ProbeTool()
    result = await execute_as(_registry(tool), identity, thread_id, tool.name)
    dispatch = db.execute(
        """SELECT a.node_id FROM plan_tool_dispatches d
           JOIN plan_node_attempts a ON a.id=d.attempt_id""",
    ).fetchone()

    assert result == "ok"
    assert dispatch["node_id"] == agent_task.id
    db.close()


def _accepted_work_holding_the_slot(db, *, second_active: bool):
    """Accept `First` while the owner's Turn still owns one of its tool calls.

    The raw transition skips release, leaving the phantom attempt that
    releases deferred on an open call wrote before #2705 (production still
    holds such rows until their Turn ends). Returns the plan and both tasks. `Second` is pending, or with
    ``second_active`` already active and queued behind First's reserved slot.
    The Turn `turn-crashed` stays running until the test ends it.
    """
    open_turn(db, "owner", "turn-crashed")
    plan = service.ensure_plan("thread", "owner", db=db)
    first = _task(db, plan.id, "First")
    second = _task(db, plan.id, "Second")
    active = _mutate(
        db, plan.id, "activate",
        {"node_id": first.id, "expected_revision": first.revision},
        "activate-first",
    )
    attempt_id = active["admission"]["selection"]["attempt_id"]
    db.execute(
        """INSERT INTO plan_tool_dispatches
           (id,attempt_id,turn_id,tool_name,started_at)
           VALUES ('stuck',?,'turn-crashed','probe','t')""",
        (attempt_id,),
    )
    if second_active:
        service.transition_node(second.id, "active", db=db)
    submit_review_evidence(first.id, db=db)
    service.transition_node(
        first.id, "accepted", verification_result="passed",
        verified_by="reviewer", db=db,
    )
    return plan, first, second


def _end_crashed_turn(db) -> None:
    """End the crashed Turn through its sole terminal owner.

    `finish_turn` settles every dispatch the Turn still owns inside the
    terminal write, which is what frees a slot held by settled work.
    """
    from infra.thread_driver import TerminalResult, finish_turn

    db.commit()
    settlement = finish_turn(
        "turn-crashed", outcome="error", ended_at="t-crash",
        successor_turn_id="turn-after-crash", boot_epoch="test",
        error="backend restarted", db=db,
    )
    assert settlement.result is TerminalResult.APPLIED


def _open_slot_nodes(db, plan_id: str) -> list[str]:
    return [
        row["node_id"]
        for row in db.execute(
            """SELECT a.node_id FROM plan_node_attempts a
               WHERE a.plan_id=? AND a.released_at IS NULL""",
            (plan_id,),
        ).fetchall()
    ]


def test_a_crashed_dispatch_frees_its_slot_and_both_paths_agree(db):
    """Settled work left holding a slot agrees on every path, and release frees it.

    Settled work whose attempt was never released keeps its slot, and the
    unique index on (plan_id, executor_thread_id) reserves that slot at the
    database level. While the phantom exists, the read path
    (`_conflict_index`) and the write path (`selection_conflict`) must give
    the same answer, or the plan surface promises a move the database then
    refuses. A crashed call on it never stops `release` (#2705).
    """
    plan, first, second = _accepted_work_holding_the_slot(db, second_active=False)

    # The agent moves on, and is refused because the slot really is still
    # reserved — the database says so and cannot be argued with.
    _mutate(
        db, plan.id, "activate",
        {"node_id": second.id, "expected_revision": second.revision},
        "activate-second",
    )
    second_row = service._node_row(db, second.id)
    projected = admission.project(second.id, db=db)
    write_path = admission_attempts.selection_conflict(db, second_row, "owner")

    # All three opinions have to match: the unique index, the read path and
    # the write path. Claiming the slot was free while the index refused the
    # insert is what produced a remedy telling the agent to start the task,
    # followed by "task is not ready to start", forever.
    assert projected["conflict"]["selected_node_id"] == first.id
    assert write_path["selected_node_id"] == first.id
    assert second_row["state"] == "pending"

    # And the conflict has to carry the occupant's real state: it is already
    # accepted, so nothing the agent does to it can free the slot.
    assert projected["conflict"]["selected_node_state"] == "accepted"

    # The crashed call is still open, and release frees the slot anyway.
    assert admission.release(first.id, "accepted", db=db) is True
    db.commit()

    # The slot is genuinely free now, and activating works.
    assert _open_slot_nodes(db, plan.id) == []
    recovered = admission.project(second.id, db=db)
    assert recovered["conflict"] is None
    assert recovered["condition"] == "ready"
    resumed = _mutate(
        db, plan.id, "activate",
        {"node_id": second.id, "expected_revision": second_row["revision"]},
        "activate-second-after-drain",
    )
    assert resumed["admission"]["condition"] == "running"
    assert resumed["admission"]["selection"] is not None


def test_a_crashed_dispatch_hands_its_slot_to_already_active_queued_work(db):
    """Active work queued behind the phantom takes the slot at Turn terminal.

    `Second` is already active, so nothing about it needs restating: the Turn
    terminal releases First's settled attempt and reconciles the plan in the
    same write. Second is running with an open attempt without `activate`,
    `start`, a sweep, or another Turn.
    """
    plan, first, second = _accepted_work_holding_the_slot(db, second_active=True)
    queued = admission.project(second.id, db=db)
    assert queued["condition"] == "ready"
    assert queued["conflict"]["selected_node_id"] == first.id

    db.commit()
    _end_crashed_turn(db)

    selected = admission.project(second.id, db=db)
    assert _open_slot_nodes(db, plan.id) == [second.id]
    assert selected["condition"] == "running"
    assert selected["conflict"] is None
    assert selected["selection"]["attempt_id"] is not None
    assert db.execute(
        "SELECT release_reason FROM plan_node_attempts WHERE node_id=?",
        (first.id,),
    ).fetchone()[0] == "turn_terminal"
    assert db.execute("SELECT COUNT(*) FROM plan_node_holds").fetchone()[0] == 0
