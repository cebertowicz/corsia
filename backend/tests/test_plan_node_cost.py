"""Per-node token, cost, and execution-time roll-up (#1840, design Slice F).

Fail-first: every assertion here demonstrates a fact `operator_node_state`
used to get wrong -- every node showing the whole commitment's total, and no
per-node figure existing at all -- before `node_cost` became the one read
owner for it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from infra.schema import SCHEMA_SQL
from plans import (
    commitment_cost, mutations, node_cost, operator_node_state, rejection, review_evidence,
    rollup, runner_events, service,
)
from tests.plan_writer_authorizer import raw_plan_seed


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project-1','Project','t','t')"
    )
    for thread_id, spawned_by in (("owner", None), ("owner2", None), ("child", "owner")):
        connection.execute(
            """INSERT INTO threads (id,user_id,project_id,spawned_by,model,created_at,updated_at)
               VALUES (?,'user-1','project-1',?,'test/model','t','t')""",
            (thread_id, spawned_by),
        )
    connection.commit()
    yield connection
    connection.close()


def _task(db, plan_id, title, *, parent_id=None, **kwargs):
    return service.add_node(
        plan_id, kind="task", title=title, acceptance_criteria=f"{title} DOD",
        parent_id=parent_id, db=db, **kwargs,
    )


def _usage(db, node, *, tokens, cost, usage_id, thread_id="owner"):
    db.execute(
        """INSERT INTO llm_usage (
               id,thread_id,turn_id,model,total_tokens,cost_usd,
               plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,
               plan_usage_role,source,created_at
           ) VALUES (?,?,NULL,'test-model',?,?,?,?,?,?,'execution','test','t')""",
        (
            usage_id, thread_id, tokens, cost,
            node.plan_id, node.id, f"attempt-{usage_id}", node.commitment_id,
        ),
    )
    db.commit()


def _turn(db, node, turn_id, *, started, ended, status_="completed", thread_id="owner"):
    db.execute(
        """INSERT INTO active_turns (
               id,thread_id,execution_epoch,model,status,started_at,ended_at,boot_epoch,
               plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role
           ) VALUES (?,?,0,'test-model',?,?,?,'boot',?,?,?,?,'execution')""",
        (
            turn_id, thread_id, status_, started, ended,
            node.plan_id, node.id, f"attempt-{turn_id}", node.commitment_id,
        ),
    )
    db.commit()


def test_sibling_tasks_roll_up_distinct_usage_and_their_parent_is_the_sum(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = service.add_node(
        plan.id, kind="phase", title="Phase", acceptance_criteria="Phase DOD", db=db,
    )
    left = _task(db, plan.id, "Left", parent_id=phase.id)
    right = _task(db, plan.id, "Right", parent_id=phase.id)
    _usage(db, left, tokens=100, cost=0.10, usage_id="u-left")
    _usage(db, right, tokens=250, cost=0.25, usage_id="u-right")

    costs = node_cost.project_many([plan.id], db=db)[plan.id]

    assert costs[left.id]["rolled"]["tokens"]["known"] == 100
    assert costs[right.id]["rolled"]["tokens"]["known"] == 250
    assert costs[left.id]["rolled"] != costs[right.id]["rolled"]
    assert costs[phase.id]["rolled"]["tokens"]["known"] == 350
    assert costs[phase.id]["rolled"]["model_cost_usd"]["known"] == pytest.approx(0.35)
    # The phase never ran a call itself; its own figure stays at zero known.
    assert costs[phase.id]["own"]["tokens"] == {"known": 0, "unknown_rows": 0}


def test_summed_costs_keep_every_digit_of_the_doubles_sql_adds(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    tenths = _task(db, plan.id, "Tenths")
    thirds = _task(db, plan.id, "Thirds")
    for index in range(10):
        _usage(db, tenths, tokens=1, cost=0.1, usage_id=f"tenth-{index}")
    for index, cost in enumerate((1 / 3, 2 / 3, 1e-7, 123456.789)):
        _usage(db, thirds, tokens=2, cost=cost, usage_id=f"third-{index}")

    costs = node_cost.project_many([plan.id], db=db)[plan.id]
    commitment = commitment_cost.project_many([tenths.commitment_id, thirds.commitment_id], db=db)

    assert costs[tenths.id]["own"]["model_cost_usd"] == {"known": 1.0, "unknown_rows": 0}
    assert costs[thirds.id]["own"]["model_cost_usd"] == {"known": 123457.7890001, "unknown_rows": 0}
    assert commitment[tenths.commitment_id]["model_cost_usd"] == {"known": 1.0, "unknown_rows": 0}
    assert commitment[thirds.commitment_id]["model_cost_usd"] == {
        "known": 123457.7890001, "unknown_rows": 0,
    }


def test_the_pane_payload_carries_the_exact_summed_costs(db):
    from plans import operator_projection
    from plans.boundary import PlanActor

    plan = service.ensure_plan("thread", "owner", db=db)
    thirds = _task(db, plan.id, "Thirds")
    for index, cost in enumerate((1 / 3, 2 / 3, 1e-7, 123456.789)):
        _usage(db, thirds, tokens=2, cost=cost, usage_id=f"third-{index}")

    payload = operator_projection.read_thread(
        "owner", actor=PlanActor(None, "user-1", None, kind="user"), db=db,
    )

    node = next(item for item in payload["plans"][0]["nodes"] if item["id"] == thirds.id)
    assert node["node_cost"]["own"]["tokens"] == {"known": 8, "unknown_rows": 0}
    assert node["node_cost"]["own"]["model_cost_usd"] == {"known": 123457.7890001, "unknown_rows": 0}
    assert node["commitment_cost"]["model_cost_usd"] == {"known": 123457.7890001, "unknown_rows": 0}


def test_the_cost_reads_step_their_rows_in_one_statement_each(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    nodes = [_task(db, plan.id, f"Task {index}") for index in range(30)]
    for index, node in enumerate(nodes):
        _usage(db, node, tokens=1, cost=0.01 * (index + 1), usage_id=f"u-{index}")
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    try:
        node_cost.project_many([plan.id], db=db)
        commitment_cost.project_many([node.commitment_id for node in nodes], db=db)
    finally:
        db.set_trace_callback(None)

    # A plain usage read would step one row per node and Turn; both run packed.
    assert not [
        s for s in statements
        if s.lstrip().startswith("SELECT") and "SUM(cost_usd)" in s
        and not s.rstrip().endswith("LIMIT 0")
    ]


def _submit(db, node_id, *, actor):
    current = service.get_node(node_id, db=db)
    with service._write(db):
        review_evidence.submit(
            node_id, evidence="done", expected_revision=current.revision,
            actor_id=actor, provenance="plan_mutation", db=db,
        )


def test_a_rejected_step_and_its_revision_count_their_shared_worker_once(db):
    """A rejection attaches the same worker to the revision; its usage lands once."""
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = service.add_node(plan.id, kind="phase", title="Phase", acceptance_criteria="d", db=db)
    step = _task(db, plan.id, "Delegate", parent_id=phase.id, spawn_kind="claude-code")
    service.transition_node(step.id, "active", db=db)
    service.ensure_plan("thread", "child", db=db)
    mutations.attach_spawned_thread(step.id, "child", db=db)
    child_plan_row = db.execute(
        "SELECT * FROM plans WHERE owner_kind='thread' AND owner_id='child'"
    ).fetchone()
    child_task = _task(db, child_plan_row["id"], "Child work")
    _usage(db, child_task, tokens=500, cost=0.50, usage_id="u-child", thread_id="child")
    service.transition_node(child_task.id, "active", db=db)
    _submit(db, child_task.id, actor="child")
    service.transition_node(
        child_task.id, "accepted", verification_result="passed", verified_by="owner", db=db,
    )
    _submit(db, step.id, actor="owner")
    _rejected, revision, _replayed = rejection.reject_submission(
        step.id, feedback="Not yet", actor_id="owner", db=db,
    )

    costs = node_cost.project_many([plan.id], db=db)[plan.id]

    assert revision.spawned_thread_id == "child"
    assert costs[step.id]["rolled"]["tokens"]["known"] == 0
    assert costs[revision.id]["rolled"]["tokens"]["known"] == 500
    assert costs[phase.id]["rolled"]["tokens"]["known"] == 500


def test_no_call_reads_zero_but_an_unmeasured_call_stays_unknown(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    idle = _task(db, plan.id, "Idle")
    unmeasured = _task(db, plan.id, "Unmeasured")
    _usage(db, unmeasured, tokens=None, cost=None, usage_id="u-unmeasured")

    costs = node_cost.project_many([plan.id], db=db)[plan.id]

    assert costs[idle.id]["own"]["tokens"] == {"known": 0, "unknown_rows": 0}
    assert costs[idle.id]["own"]["model_cost_usd"] == {"known": 0, "unknown_rows": 0}
    assert costs[unmeasured.id]["own"]["tokens"] == {"known": None, "unknown_rows": 1}
    assert costs[unmeasured.id]["own"]["model_cost_usd"] == {"known": None, "unknown_rows": 1}


@pytest.mark.parametrize(
    ("status_", "ended"),
    [
        ("running", None),
        ("waiting_retry", None),
        ("completed", "2026-09-23T00:01:00+00:00"),
    ],
)
def test_a_turn_with_no_usage_row_yet_stays_unknown_not_zero(db, status_, ended):
    """A bound Turn means a call began; only a node no Turn ever ran reads zero."""
    from plans import commitment_cost

    plan = service.ensure_plan("thread", "owner", db=db)
    phase = service.add_node(plan.id, kind="phase", title="Phase", acceptance_criteria="d", db=db)
    worked = _task(db, plan.id, "Worked", parent_id=phase.id)
    _turn(db, worked, "t-worked", started="2026-09-23T00:00:00+00:00", ended=ended,
          status_="running" if status_ == "waiting_retry" else status_)
    if status_ == "waiting_retry":
        db.execute(
            "UPDATE active_turns SET status='waiting_retry', retry_at=? WHERE id='t-worked'",
            ("2026-09-23T00:05:00+00:00",),
        )
        db.commit()

    costs = node_cost.project_many([plan.id], db=db)[plan.id]
    total = commitment_cost.project_many([phase.commitment_id], db=db)[phase.commitment_id]

    assert costs[worked.id]["own"]["model_cost_usd"]["known"] is None
    assert costs[phase.id]["rolled"]["tokens"]["known"] is None
    assert costs[phase.id]["own"]["tokens"]["known"] == 0
    assert total["model_cost_usd"]["known"] is None


def test_a_lost_call_and_its_parent_stay_unknown_not_zero(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = service.add_node(plan.id, kind="phase", title="Phase", acceptance_criteria="d", db=db)
    lost = _task(db, plan.id, "Lost", parent_id=phase.id)
    runner_events.record(
        lost.id, "usage_recording_failed", provenance="test.node_cost",
        payload={"turn_id": "t-lost", "usage_key": "call-1", "reason": "unreadable"},
        db=db,
    )
    db.commit()

    costs = node_cost.project_many([plan.id], db=db)[plan.id]

    assert costs[lost.id]["own"]["tokens"]["known"] is None
    assert costs[phase.id]["rolled"]["tokens"]["known"] is None
    assert costs[phase.id]["own"]["tokens"]["known"] == 0


@pytest.mark.parametrize("measured_cost", [0.0, 0.01])
def test_a_lost_call_beside_a_measured_one_keeps_the_measured_total(db, measured_cost):
    """A measured zero (local models cost $0) rolls up exactly like any other amount."""
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = service.add_node(plan.id, kind="phase", title="Phase", acceptance_criteria="d", db=db)
    measured = _task(db, plan.id, "Measured", parent_id=phase.id)
    lost = _task(db, plan.id, "Lost", parent_id=phase.id)
    _usage(db, measured, tokens=10, cost=measured_cost, usage_id="u-measured")
    runner_events.record(
        lost.id, "usage_recording_failed", provenance="test.node_cost",
        payload={"turn_id": "t-lost", "usage_key": "call-1", "reason": "unreadable"},
        db=db,
    )
    db.commit()

    rolled = node_cost.project_many([plan.id], db=db)[plan.id][phase.id]["rolled"]

    assert rolled["model_cost_usd"]["known"] == pytest.approx(measured_cost)
    assert rolled["usage_recording_failures"] == 1


def test_attached_child_plans_usage_rolls_into_the_delegating_task(db):
    parent_plan = service.ensure_plan("thread", "owner", db=db)
    delegate = _task(db, parent_plan.id, "Delegate", spawn_kind="claude-code")
    service.transition_node(delegate.id, "active", db=db)
    service.ensure_plan("thread", "child", db=db)
    mutations.attach_spawned_thread(delegate.id, "child", db=db)
    child_plan_row = db.execute(
        "SELECT * FROM plans WHERE owner_kind='thread' AND owner_id='child'"
    ).fetchone()
    child_task = _task(db, child_plan_row["id"], "Child work")
    _usage(db, child_task, tokens=500, cost=0.50, usage_id="u-child", thread_id="child")

    costs = node_cost.project_many([parent_plan.id], db=db)[parent_plan.id]

    assert costs[delegate.id]["own"]["tokens"]["known"] == 0
    assert costs[delegate.id]["rolled"]["tokens"]["known"] == 500
    assert costs[delegate.id]["rolled"]["model_cost_usd"]["known"] == pytest.approx(0.50)


def test_a_child_requested_beside_its_archived_delegating_step_folds_once(db):
    """#2077: the child is a top-level plan and still attached below the step."""
    parent_plan = service.ensure_plan("thread", "owner", db=db)
    delegate = _task(db, parent_plan.id, "Delegate", spawn_kind="claude-code")
    service.transition_node(delegate.id, "active", db=db)
    mutations.attach_spawned_thread(delegate.id, "child", db=db)
    child_plan = service.ensure_plan("thread", "child", db=db)
    child_task = _task(db, child_plan.id, "Child work")
    _usage(db, child_task, tokens=500, cost=0.50, usage_id="u-child", thread_id="child")
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET archived_at='t' WHERE id=?", (delegate.id,))

    costs = node_cost.project_many([parent_plan.id, child_plan.id], db=db)

    assert costs[parent_plan.id][delegate.id]["rolled"]["tokens"]["known"] == 500
    assert costs[child_plan.id][child_task.id]["rolled"]["tokens"]["known"] == 500


def test_top_level_rolled_tokens_equal_the_commitment_total_with_no_double_counting(db):
    """The delegate is its commitment's one top-level node; its rolled total --
    own plus the attached child plan's whole rolled total, and the child
    plan's own descendant -- must equal the commitment's own aggregate
    exactly, with nothing counted on both sides.
    """
    parent_plan = service.ensure_plan("thread", "owner", db=db)
    delegate = _task(db, parent_plan.id, "Delegate", spawn_kind="claude-code")
    service.transition_node(delegate.id, "active", db=db)
    _usage(db, delegate, tokens=40, cost=0.04, usage_id="u-own")
    service.ensure_plan("thread", "child", db=db)
    mutations.attach_spawned_thread(delegate.id, "child", db=db)
    child_plan_row = db.execute(
        "SELECT * FROM plans WHERE owner_kind='thread' AND owner_id='child'"
    ).fetchone()
    child_root = service.add_node(
        child_plan_row["id"], kind="phase", title="Child root",
        acceptance_criteria="Child root DOD", db=db,
    )
    child_leaf = _task(db, child_plan_row["id"], "Child leaf", parent_id=child_root.id)
    _usage(db, child_root, tokens=100, cost=0.10, usage_id="u-child-root", thread_id="child")
    _usage(db, child_leaf, tokens=400, cost=0.40, usage_id="u-child-leaf", thread_id="child")

    costs = node_cost.project_many([parent_plan.id], db=db)[parent_plan.id]
    rolled_sum = costs[delegate.id]["rolled"]["tokens"]["known"]
    total = commitment_cost.project(delegate.id, db=db)

    assert rolled_sum == 540
    assert rolled_sum == total["tokens"]["known"]


def test_a_usage_recording_failure_counts_separately_never_folded_into_tokens(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Recorded partially")
    _usage(db, node, tokens=20, cost=0.02, usage_id="u-1")
    runner_events.record(
        node.id, "usage_recording_failed", provenance="test.node_cost",
        payload={"turn_id": "t-lost", "usage_key": "call-2", "reason": "unreadable"},
        db=db,
    )
    db.commit()

    costs = node_cost.project_many([plan.id], db=db)[plan.id][node.id]

    assert costs["own"]["tokens"]["known"] == 20
    assert costs["own"]["usage_recording_failures"] == 1
    assert costs["rolled"]["usage_recording_failures"] == 1
    assert costs["rolled"]["tokens"]["known"] == 20


def test_a_running_turn_counts_to_the_clock_and_a_terminal_turn_without_an_end_is_unknown(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Timed work")
    now = datetime(2026, 9, 17, 0, 0, tzinfo=timezone.utc)
    _turn(
        db, node, "t-running",
        started=(now - timedelta(seconds=30)).isoformat(), ended=None, status_="running",
    )
    _turn(
        db, node, "t-unmeasurable",
        started=(now - timedelta(minutes=5)).isoformat(), ended=None, status_="completed",
    )

    costs = node_cost.project_many([plan.id], db=db, now=now)[plan.id][node.id]

    assert costs["own"]["execution"] == {
        "seconds": 30, "unknown_turns": 1,
        "turns": {"completed": 1, "error": 0, "cancelled": 0, "running": 1, "waiting_retry": 0},
    }
    assert costs["rolled"]["execution"] == costs["own"]["execution"]


def test_commitment_cost_is_present_only_on_the_commitment_root_node(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    phase = service.add_node(
        plan.id, kind="phase", title="Phase", acceptance_criteria="Phase DOD", db=db,
    )
    child = _task(db, plan.id, "Child", parent_id=phase.id)
    db.commit()

    projected = rollup.rollup_thread(plan.id, db=db)
    states = operator_node_state.project([projected], {}, db=db)

    assert states[phase.id]["commitment_cost"]["commitment_id"] == phase.id
    assert states[child.id]["commitment_cost"] is None
    assert states[child.id]["node_cost"]["own"]["tokens"] == {"known": 0, "unknown_rows": 0}
    assert "node_cost" in states[phase.id]


def _select_statements(db, fn) -> int:
    statements: list[str] = []
    db.set_trace_callback(
        lambda statement: (
            statements.append(statement)
            if statement.lstrip().upper().startswith(("SELECT", "WITH"))
            and not statement.rstrip().endswith(") LIMIT 0")
            else None
        )
    )
    try:
        fn()
    finally:
        db.set_trace_callback(None)
    return len(statements)


def test_project_many_query_count_is_independent_of_node_count(db):
    small_plan = service.ensure_plan("thread", "owner", db=db)
    for index in range(5):
        _task(db, small_plan.id, f"Small {index}")
    db.commit()
    small_count = _select_statements(
        db, lambda: node_cost.project_many([small_plan.id], db=db),
    )

    large_plan = service.ensure_plan("thread", "owner2", db=db)
    for index in range(200):
        _task(db, large_plan.id, f"Large {index}")
    db.commit()
    large_count = _select_statements(
        db, lambda: node_cost.project_many([large_plan.id], db=db),
    )

    assert large_count == small_count


def test_a_plan_wider_than_one_id_chunk_still_counts_every_node(db, monkeypatch):
    """The host-parameter limit bounds the chunk, never the plan's width.

    SQLite refuses a query with more host parameters than its compiled limit
    (999 on older builds), and the operator projection batches every plan in a
    workspace, so the id lists are chunked. Chunking must not lose a node or
    double-count one.
    """
    monkeypatch.setattr(node_cost, "_ID_CHUNK", 3)
    plan = service.ensure_plan("thread", "owner", db=db)
    nodes = [_task(db, plan.id, f"Task {index}") for index in range(10)]
    for index, node in enumerate(nodes):
        _usage(db, node, tokens=index + 1, cost=0.0, usage_id=f"u{index}")
    db.commit()

    projected = node_cost.project_many([plan.id], db=db)[plan.id]

    assert len(projected) == len(nodes)
    assert [
        projected[node.id]["own"]["tokens"]["known"] for node in nodes
    ] == list(range(1, 11))


def test_chunking_costs_queries_per_chunk_never_per_node(db, monkeypatch):
    monkeypatch.setattr(node_cost, "_ID_CHUNK", 3)
    plan = service.ensure_plan("thread", "owner", db=db)
    for index in range(3):
        _task(db, plan.id, f"One chunk {index}")
    db.commit()
    one_chunk = _select_statements(db, lambda: node_cost.project_many([plan.id], db=db))

    wider = service.ensure_plan("thread", "owner2", db=db)
    for index in range(12):
        _task(db, wider.id, f"Four chunks {index}")
    db.commit()
    four_chunks = _select_statements(db, lambda: node_cost.project_many([wider.id], db=db))

    assert four_chunks < one_chunk * 4


def test_nesting_deeper_than_the_interpreter_stack_still_rolls_up(db):
    """Every plan write projects cost, so a deep branch must never overflow the fold.

    The rows are copied from one real node so the setup stays fast; each is
    a phase under the one before it.
    """
    plan = service.ensure_plan("thread", "owner", db=db)
    seed = dict(db.execute(
        "SELECT * FROM plan_nodes WHERE id=?",
        (service.add_node(
            plan.id, kind="phase", title="Depth 0", acceptance_criteria="d", db=db,
        ).id,),
    ).fetchone())
    columns = ",".join(seed)
    marks = ",".join("?" for _ in seed)
    parent_id = seed["id"]
    for depth in range(1, 1500):
        row = dict(seed, id=f"deep-{depth}", parent_id=parent_id, title=f"Depth {depth}")
        with raw_plan_seed(db):
            db.execute(f"INSERT INTO plan_nodes ({columns}) VALUES ({marks})", tuple(row.values()))
        parent_id = row["id"]
    deepest = service.get_node(parent_id, db=db)
    _usage(db, deepest, tokens=7, cost=0.0, usage_id="u-deep")

    costs = node_cost.project_many([plan.id], db=db)[plan.id]

    assert len(costs) == 1500
    assert costs[seed["id"]]["rolled"]["tokens"]["known"] == 7


def test_delegation_deeper_than_the_interpreter_stack_still_rolls_up(db):
    """A chain of workers each delegating to the next folds without recursion."""
    root = service.ensure_plan("thread", "owner", db=db)
    plan, parent_thread = root, "owner"
    for depth in range(700):
        thread_id = f"worker-{depth}"
        db.execute(
            """INSERT INTO threads (id,user_id,project_id,spawned_by,model,created_at,updated_at)
               VALUES (?,'user-1','project-1',?,'test/model','t','t')""",
            (thread_id, parent_thread),
        )
        step = _task(db, plan.id, f"Delegate {depth}")
        child_plan = service.ensure_plan("thread", thread_id, db=db)
        mutations.attach_spawned_thread(step.id, thread_id, db=db)
        plan = child_plan
        parent_thread = thread_id
    leaf = _task(db, plan.id, "Leaf work")
    _usage(db, leaf, tokens=7, cost=0.0, usage_id="u-leaf", thread_id=parent_thread)
    db.commit()

    costs = node_cost.project_many([root.id], db=db)

    assert len(costs) == 701
    (first_step,) = costs[root.id].values()
    assert first_step["rolled"]["tokens"]["known"] == 7
