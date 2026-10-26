"""Turn-bound usage and cumulative original-commitment cost."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import sqlite3

import pytest

from infra.llm_usage import (
    reconcile_pending_turn_usage,
    record_llm_usage,
)
from infra.schema import SCHEMA_SQL
from llm.api_equivalent import api_equivalent
from plans import (
    admission,
    commitment_cost,
    corrections,
    mutations,
    review_usage,
    service,
)
from plans.boundary import PlanActor, mutate


class _KeepOpen:
    def __init__(self, db):
        self.db = db

    def __getattr__(self, name):
        return getattr(self.db, name)

    def close(self):
        return None


class _FailUsageInsert(_KeepOpen):
    def execute(self, sql, params=()):
        if "INSERT INTO llm_usage" in sql:
            raise sqlite3.OperationalError("usage insert failed")
        return self.db.execute(sql, params)


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    for thread_id, spawned_by in (("owner", None), ("child", "owner")):
        connection.execute(
            "INSERT INTO threads (id,user_id,spawned_by,created_at,updated_at) "
            "VALUES (?,'user',?,'2026-09-09T00:00:00+00:00',"
            "'2026-09-09T00:00:00+00:00')",
            (thread_id, spawned_by),
        )
    connection.commit()
    yield connection
    connection.close()


#: A model with a public list price, so its calls carry an API-equivalent price.
CLAUDE = "claude-code/claude-opus-5-5"


def _call(tokens: int, cost: float) -> dict:
    """One provider call's measurement: ``tokens`` input tokens and the stored ``cost``."""
    return {
        "prompt_tokens": tokens, "completion_tokens": 0, "cache_creation_tokens": 0,
        "cache_read_tokens": 0, "total_tokens": tokens, "cost_usd": cost,
    }


def _price(tokens: int) -> float:
    return api_equivalent(CLAUDE, tokens, 0, 0, 0)


def _task(db, plan_id, title, **kwargs):
    return service.add_node(
        plan_id,
        kind="task",
        title=title,
        acceptance_criteria=f"{title} is verified",
        db=db,
        **kwargs,
    )


def _activate(db, node):
    return admission.activate(
        node.id,
        expected_revision=node.revision,
        actor_id="owner",
        provenance="test.commitment_cost",
        db=db,
    )


def _turn(db, turn_id, thread_id, attempt, node, *, status="running"):
    db.execute(
        """INSERT INTO active_turns (
               id,thread_id,execution_epoch,model,status,started_at,boot_epoch,
               plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,
               plan_usage_role
           ) VALUES (?,?,0,'test-model',?,'2026-09-09T00:00:00+00:00',
                     'test-boot',?,?,?,?,?)""",
        (
            turn_id,
            thread_id,
            status,
            node.plan_id,
            node.id,
            attempt["id"],
            node.commitment_id,
            "execution",
        ),
    )
    db.commit()


def _unbound_turn(db, turn_id, thread_id):
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES (?,?,0,'test-model','running',
                   '2026-09-09T00:00:00+00:00','test-boot')""",
        (turn_id, thread_id),
    )
    db.commit()


def test_plan_bound_usage_snapshots_turn_binding_and_preserves_unknowns(db, monkeypatch):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Original commitment")
    active = _activate(db, node)
    attempt = db.execute(
        "SELECT * FROM plan_node_attempts WHERE id=?",
        (active["admission"]["selection"]["attempt_id"],),
    ).fetchone()
    current = service.get_node(node.id, db=db)
    _turn(db, "turn-1", "owner", attempt, current)
    monkeypatch.setattr("infra.database.get_db", lambda: _KeepOpen(db))

    record_llm_usage(
        "owner",
        CLAUDE,
        _call(12, 0.25),
        turn_id="turn-1",
        usage_key="provider-call:1",
        _emit_product_event=lambda *_args, **_kwargs: None,
    )
    record_llm_usage(
        "owner",
        "test-model",
        {},
        turn_id="turn-1",
        usage_key="provider-call:2",
        _emit_product_event=lambda *_args, **_kwargs: None,
    )

    rows = db.execute(
        """SELECT turn_id,plan_id,plan_node_id,plan_attempt_id,
                  plan_commitment_id,plan_usage_role,total_tokens,cost_usd
           FROM llm_usage ORDER BY created_at,id"""
    ).fetchall()
    assert len(rows) == 2
    assert tuple(rows[0]) == (
        "turn-1",
        plan.id,
        node.id,
        attempt["id"],
        node.commitment_id,
        "execution",
        12,
        0.25,
    )
    assert rows[1]["total_tokens"] is None
    assert rows[1]["cost_usd"] is None

    projected = commitment_cost.project(
        node.commitment_id,
        db=db,
        now=datetime(2026, 9, 9, 0, 1, tzinfo=timezone.utc),
    )
    assert projected["tokens"] == {"known": 12, "unknown_rows": 1}
    assert projected["model_cost_usd"] == {"known": pytest.approx(_price(12)), "unknown_rows": 1}
    assert projected["attempts"] == {
        "total": 1,
        "failed": 0,
        "correction": 0,
        "delegated": 0,
    }
    assert projected["elapsed_seconds"]["status"] == "in_progress"


def test_checkpoint_spend_joins_original_commitment_without_a_synthetic_turn(db):
    from infra import checkpoints, checkpoint_usage
    from plans.usage_calls import call_binding
    from tests.test_checkpoint_store import _request

    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Checkpoint attribution")
    _activate(db, node)
    for seq in range(1, 4):
        db.execute("INSERT INTO messages (id,thread_id,role,content,seq,created_at) "
                   "VALUES (?, 'owner', 'assistant', 'retained', ?, 't0')", (f"owner-m{seq}", seq))
    db.commit()
    checkpoints.create_running(_request("owner", "cp-plan"), db=db)
    checkpoint_usage.freeze_binding("cp-plan", call_binding("owner", db=db), db=db)
    checkpoint_usage.record_call("cp-plan", "call-1", "test-model", {
        "total_tokens": 150, "cost_usd": None,
    }, now_iso="2026-09-09T00:01:00+00:00", db=db)
    projected = commitment_cost.project(node.commitment_id, db=db)
    assert projected["tokens"] == {"known": 150, "unknown_rows": 0}
    assert projected["model_cost_usd"] == {"known": None, "unknown_rows": 1}
    assert db.execute("SELECT COUNT(*) FROM active_turns").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM plan_node_attempts").fetchone()[0] == 1


def test_completed_effect_terminal_repair_does_not_invent_commitment_cost(db):
    from core.turn_core import persist_turn_terminal
    from infra.thread_driver import TerminalResult
    from subagents.child_thread import persist_assistant_step
    from tools.recorded_call import CompletedToolCall, CompletedToolFailure

    plan = service.ensure_plan("thread", "owner", db=db)
    db.execute("UPDATE threads SET model='test-model' WHERE id='owner'")
    db.commit()
    node = _task(db, plan.id, "Retained completed effect")
    active = _activate(db, node)
    attempt = db.execute(
        "SELECT * FROM plan_node_attempts WHERE id=?",
        (active["admission"]["selection"]["attempt_id"],),
    ).fetchone()
    current = service.get_node(node.id, db=db)
    _turn(db, "turn-effect", "owner", attempt, current)
    step = persist_assistant_step("owner", content="", tool_uses=[{
        "id": "call", "name": "probe", "arguments": {},
    }], turn_id="turn-effect", db=db)
    db.commit()
    failure = CompletedToolFailure((CompletedToolCall(
        name="probe", arguments={}, result="one completed output",
        thread_id="owner", turn_id="turn-effect", invocation_id="invocation",
        provider_tool_call_id="call", assistant_step_id=step,
    ),))

    assert persist_turn_terminal("turn-effect", "error", failure, db=db) is TerminalResult.APPLIED

    projected = commitment_cost.project(node.commitment_id, db=db)
    assert projected["attempts"] == {
        "total": 1, "failed": 0, "correction": 0, "delegated": 0,
    }
    assert projected["tokens"] == {"known": None, "unknown_rows": 1}
    assert db.execute("SELECT COUNT(*) FROM plan_node_attempts").fetchone()[0] == 1
    assert db.execute(
        "SELECT content FROM messages WHERE role='tool'",
    ).fetchone()[0] == "one completed output"


def test_a_failed_usage_write_is_held_and_counted_unknown_never_raised(db, monkeypatch):
    """A write the ledger refused is owed, not lost: bound or not, it waits for its Turn."""
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Bound work")
    active = _activate(db, node)
    attempt = db.execute(
        "SELECT * FROM plan_node_attempts WHERE id=?",
        (active["admission"]["selection"]["attempt_id"],),
    ).fetchone()
    current = service.get_node(node.id, db=db)
    _turn(db, "turn-bound", "owner", attempt, current)
    monkeypatch.setattr("infra.database.get_db", lambda: _FailUsageInsert(db))

    record_llm_usage(
        "owner",
        "test-model",
        {"total_tokens": 4},
        turn_id="turn-bound",
        usage_key="provider-call:1",
        _emit_product_event=lambda *_args, **_kwargs: None,
    )
    record_llm_usage(
        "owner",
        "test-model",
        {"total_tokens": 4},
        _emit_product_event=lambda *_args, **_kwargs: None,
    )

    projected = commitment_cost.project(node.commitment_id, db=db)
    assert projected["tokens"] == {"known": None, "unknown_rows": 1}


def test_review_turn_binds_only_previously_unattributed_usage(db, monkeypatch):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Review attribution")
    active = _activate(db, node)
    reviewed = mutations.request_review(
        node.id,
        evidence="Focused proof passed",
        expected_revision=active["node"]["revision"],
        actor_id="owner",
        provenance="test.commitment_cost",
        db=db,
    )
    admission.release(node.id, "review_requested", db=db)
    _unbound_turn(db, "review-turn", "owner")
    monkeypatch.setattr("infra.database.get_db", lambda: _KeepOpen(db))
    record_llm_usage(
        "owner",
        "test-model",
        {"total_tokens": 9},
        turn_id="review-turn",
        usage_key="provider-call:1",
        _emit_product_event=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr("infra.database.get_db", lambda: _FailUsageInsert(db))
    record_llm_usage(
        "owner",
        "test-model",
        {"total_tokens": 11},
        turn_id="review-turn",
        usage_key="provider-call:2",
        _emit_product_event=lambda *_args, **_kwargs: None,
    )

    review_usage.bind(
        node.id,
        reviewer_thread_id="owner",
        turn_id="review-turn",
        db=db,
    )
    assert reconcile_pending_turn_usage(db) == 1

    rows = db.execute(
        """SELECT plan_node_id,plan_commitment_id,plan_usage_role
           FROM llm_usage WHERE turn_id='review-turn' ORDER BY total_tokens"""
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (node.id, node.commitment_id, "verification"),
        (node.id, node.commitment_id, "verification"),
    ]
    turn = db.execute(
        "SELECT plan_usage_role FROM active_turns WHERE id='review-turn'"
    ).fetchone()
    assert turn["plan_usage_role"] == "verification"
    assert reviewed.state == "review_requested"


def test_delegated_and_correction_attempts_stay_on_the_original_commitment(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    parent = _task(db, plan.id, "Delegated commitment", spawn_kind="delegated")
    _activate(db, parent)

    child_plan = service.ensure_plan("thread", "child", db=db)
    child_task = _task(db, child_plan.id, "Delegated work")
    mutations.attach_spawned_thread(
        parent.id,
        "child",
        expected_revision=service.get_node(parent.id, db=db).revision,
        actor_id="owner",
        provenance="test.commitment_cost",
        db=db,
    )
    child_task = service.get_node(child_task.id, db=db)
    _activate(db, child_task)

    repeat_source = mutations.split_node(
        parent.id,
        title="Failed attempt",
        acceptance_criteria="Failure is recorded",
        expected_revision=service.get_node(parent.id, db=db).revision,
        actor_id="owner",
        provenance="test.commitment_cost",
        db=db,
    )
    service.transition_node(repeat_source.id, "failed", db=db)
    affected = _task(db, plan.id, "Affected work")
    corrected = corrections.add(
        repeat_source.id,
        title="Required correction",
        acceptance_criteria="Correction is verified",
        affected_nodes=[
            {"node_id": affected.id, "expected_revision": affected.revision}
        ],
        expected_revision=service.get_node(repeat_source.id, db=db).revision,
        actor_id="owner",
        provenance="test.commitment_cost",
        db=db,
    )
    admission.release(parent.id, "superseded_by_correction", db=db)
    correction = service.get_node(corrected["correction"]["id"], db=db)
    _activate(db, correction)
    service.transition_node(correction.id, "failed", db=db)

    projected = commitment_cost.project(parent.commitment_id, db=db)
    assert projected["attempts"] == {
        "total": 3,
        "failed": 1,
        "correction": 1,
        "delegated": 1,
    }
    assert child_task.commitment_id == parent.commitment_id
    assert correction.commitment_id == parent.commitment_id


def test_user_effort_counts_each_receipted_human_plan_action_once(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Human action")
    active = _activate(db, node)
    params = {
        "node_id": node.id,
        "reason": "Operator paused work",
        "expected_revision": active["node"]["revision"],
    }
    actor = PlanActor(None, "user", None, kind="user")

    first = mutate(
        plan.id,
        "pause_node",
        params,
        actor=actor,
        provenance="test.commitment_cost",
        mutation_id="human-pause",
        db=db,
    ).result
    replay = mutate(
        plan.id,
        "pause_node",
        params,
        actor=actor,
        provenance="test.commitment_cost",
        mutation_id="human-pause",
        db=db,
    ).result

    assert replay == first
    assert commitment_cost.project(node.commitment_id, db=db)["user_effort"] == {
        "known_plan_actions": 1,
        "status": "partial",
    }


def test_completed_bound_turn_without_a_usage_row_is_unknown(db):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Unknown spend")
    active = _activate(db, node)
    attempt = db.execute(
        "SELECT * FROM plan_node_attempts WHERE id=?",
        (active["admission"]["selection"]["attempt_id"],),
    ).fetchone()
    current = service.get_node(node.id, db=db)
    _turn(db, "turn-missing", "owner", attempt, current, status="completed")

    projected = commitment_cost.project(node.commitment_id, db=db)
    assert projected["tokens"] == {"known": None, "unknown_rows": 1}
    assert projected["model_cost_usd"] == {"known": None, "unknown_rows": 1}


def test_a_pending_receipt_is_read_before_the_row_that_replaces_it(db):
    """The two reads are separate, so their order decides what a race costs.

    Projecting a call writes its ledger row and deletes its pending receipt
    in one transaction, and these are two reads on a connection holding no
    transaction of its own. One committing between them is counted by
    whichever read runs second: receipts first can only leave a call briefly
    counted as unknown that its row already covers, while rows first would
    see neither the row nor the receipt and present a total that is
    complete-looking and short.
    """
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Work whose receipt races its row")
    active = _activate(db, node)
    attempt = db.execute(
        "SELECT * FROM plan_node_attempts WHERE id=?",
        (active["admission"]["selection"]["attempt_id"],),
    ).fetchone()
    current = service.get_node(node.id, db=db)
    _turn(db, "turn-raced", "owner", attempt, current, status="completed")

    statements: list[str] = []
    db.set_trace_callback(lambda statement: statements.append(" ".join(statement.split())))
    commitment_cost.project(node.commitment_id, db=db)
    db.set_trace_callback(None)

    pending_at = next(i for i, s in enumerate(statements) if "json_each" in s)
    usage_at = next(i for i, s in enumerate(statements) if "FROM llm_usage" in s)
    assert pending_at < usage_at


def test_a_lost_plan_bound_row_is_declared_unknown_not_silently_short(
    db, caplog, monkeypatch,
):
    """A Turn is many calls, so losing one must not leave the Turn measured.

    One usage row per provider call is the ordinary agentic shape. Deciding
    measurement per Turn would call this Turn measured on the strength of its
    first call's row and present a `known` total missing its second -- the
    complete-but-short reading that `turns_util._log_llm_usage`'s catch is
    justified by never producing. The pending receipt is counted per row
    instead, and reconciliation projects the exact measurement and removes
    that temporary unknown.
    """
    from core import turns_util

    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Work whose second call is lost")
    active = _activate(db, node)
    attempt = db.execute(
        "SELECT * FROM plan_node_attempts WHERE id=?",
        (active["admission"]["selection"]["attempt_id"],),
    ).fetchone()
    current = service.get_node(node.id, db=db)
    _turn(db, "turn-partial", "owner", attempt, current, status="completed")
    monkeypatch.setattr(
        turns_util, "emit_product_event", lambda *_args, **_kwargs: None,
    )

    monkeypatch.setattr("infra.database.get_db", lambda: _KeepOpen(db))
    turns_util._log_llm_usage(
        "owner",
        CLAUDE,
        _call(11, 0.25),
        turn_id="turn-partial",
        usage_key="provider-call:1",
    )
    # Inject at the canonical factory so the failure travels the real writer.
    monkeypatch.setattr("infra.database.get_db", lambda: _FailUsageInsert(db))
    with caplog.at_level(logging.ERROR):
        turns_util._log_llm_usage(
            "owner",
            CLAUDE,
            _call(9, 0.5),
            turn_id="turn-partial",
            usage_key="provider-call:2",
        )

    # One of the Turn's two calls reached the ledger, so row presence alone
    # would have called the whole Turn measured.
    assert db.execute(
        "SELECT COUNT(*) FROM llm_usage WHERE turn_id='turn-partial'"
    ).fetchone()[0] == 1

    projected = commitment_cost.project(node.commitment_id, db=db)
    assert projected["tokens"] == {"known": 11, "unknown_rows": 1}
    assert projected["model_cost_usd"] == {"known": pytest.approx(_price(11)), "unknown_rows": 1}

    assert reconcile_pending_turn_usage(db) == 1
    recovered = commitment_cost.project(node.commitment_id, db=db)
    assert recovered["tokens"] == {"known": 20, "unknown_rows": 0}
    assert recovered["model_cost_usd"] == {
        "known": pytest.approx(_price(11) + _price(9)), "unknown_rows": 0,
    }


def test_a_lost_row_is_unknown_on_the_task_its_call_was_billed_to(
    db, caplog, monkeypatch,
):
    """A Turn that moves to a second task owes that task its later calls.

    Each call is billed to the task current when it began, and a pending
    receipt becomes a row with that binding -- so its unknown belongs there
    too, not to the task in the Turn's start snapshot. Charged by snapshot,
    the first task would show an unknown it never owed and the second a
    complete-looking total missing the call.
    """
    from core import turns_util

    plan = service.ensure_plan("thread", "owner", db=db)
    first = _task(db, plan.id, "Work the Turn started on")
    active = _activate(db, first)
    attempt = db.execute(
        "SELECT * FROM plan_node_attempts WHERE id=?",
        (active["admission"]["selection"]["attempt_id"],),
    ).fetchone()
    _turn(db, "turn-moved", "owner", attempt, service.get_node(first.id, db=db),
          status="completed")
    admission.release(first.id, "accepted", db=db)
    second = _task(db, plan.id, "Work the Turn moved on to")
    second_attempt = _activate(db, second)["admission"]["selection"]["attempt_id"]
    db.commit()
    moved = {
        "plan_id": plan.id,
        "plan_node_id": second.id,
        "plan_attempt_id": second_attempt,
        "plan_commitment_id": service.get_node(second.id, db=db).commitment_id,
        "plan_usage_role": "execution",
    }
    monkeypatch.setattr(
        turns_util, "emit_product_event", lambda *_args, **_kwargs: None,
    )

    monkeypatch.setattr("infra.database.get_db", lambda: _KeepOpen(db))
    turns_util._log_llm_usage(
        "owner", "test-model", {"total_tokens": 11, "cost_usd": 0.25},
        turn_id="turn-moved", usage_key="provider-call:1",
    )
    monkeypatch.setattr("infra.database.get_db", lambda: _FailUsageInsert(db))
    with caplog.at_level(logging.ERROR):
        turns_util._log_llm_usage(
            "owner", "test-model", {"total_tokens": 9, "cost_usd": 0.5},
            turn_id="turn-moved", usage_key="provider-call:2", binding=moved,
        )

    billed = moved["plan_commitment_id"]
    costs = commitment_cost.project_many([first.commitment_id, billed], db=db)
    assert costs[first.commitment_id]["tokens"] == {"known": 11, "unknown_rows": 0}
    assert costs[billed]["tokens"] == {"known": None, "unknown_rows": 1}

    assert reconcile_pending_turn_usage(db) == 1
    recovered = commitment_cost.project(billed, db=db)
    assert recovered["tokens"] == {"known": 9, "unknown_rows": 0}


def test_a_failed_plan_bound_projection_is_recoverable_without_false_zero(
    db, caplog, monkeypatch,
):
    """The whole path: projection fails, Turn survives, restart recovers cost.

    A usage failure must never reach `turns_util._log_llm_usage`'s terminal
    path, guarded only by try/finally: the user would never receive the turn.
    The ledger holds the call for the Turn's terminal instead, and while its
    durable receipt is pending commitment cost reports it under
    `unknown_rows`, never as zero. Reconciliation, the fallback for a process
    that exits first, projects the exact measurement and removes that unknown.
    """
    from core import turns_util

    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Work whose cost is lost")
    active = _activate(db, node)
    attempt = db.execute(
        "SELECT * FROM plan_node_attempts WHERE id=?",
        (active["admission"]["selection"]["attempt_id"],),
    ).fetchone()
    current = service.get_node(node.id, db=db)
    _turn(db, "turn-lost", "owner", attempt, current, status="completed")

    # Inject at the canonical factory so the failure travels the real writer.
    monkeypatch.setattr("infra.database.get_db", lambda: _FailUsageInsert(db))
    monkeypatch.setattr(
        turns_util, "emit_product_event", lambda *_args, **_kwargs: None,
    )

    with caplog.at_level(logging.ERROR):
        turns_util._log_llm_usage(
            "owner",
            CLAUDE,
            _call(9, 0.5),
            turn_id="turn-lost",
            usage_key="provider-call:1",
        )

    assert db.execute(
        "SELECT COUNT(*) FROM llm_usage WHERE turn_id='turn-lost'"
    ).fetchone()[0] == 0

    projected = commitment_cost.project(node.commitment_id, db=db)
    assert projected["tokens"] == {"known": None, "unknown_rows": 1}
    assert projected["model_cost_usd"] == {"known": None, "unknown_rows": 1}

    from infra.llm_usage import reconcile_pending_turn_usage

    assert reconcile_pending_turn_usage(db) == 1
    recovered = commitment_cost.project(node.commitment_id, db=db)
    assert recovered["tokens"] == {"known": 9, "unknown_rows": 0}
    assert recovered["model_cost_usd"] == {"known": pytest.approx(_price(9)), "unknown_rows": 0}
