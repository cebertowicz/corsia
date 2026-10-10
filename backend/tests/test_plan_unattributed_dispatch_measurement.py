"""Per-Turn measurement of work-tool calls with no plan task selected.

``plans.tool_dispatches.record`` returns False, writing nothing and taking no
write lock, for a started call with no valid selection and no active plan of
its own worth reconciling. That branch runs unrecorded on every started call
of every thread with nothing in progress, so nobody could measure how often
it happens. These tests cover the in-process counter that makes it visible:
one durable row per Turn, written once at Turn terminal, never a write per
call and never a row for a Turn that had a valid selection throughout.
"""

from __future__ import annotations

import pytest

from infra.database import get_db, write_transaction
from plans import admission, service, tool_dispatches
from tests.plan_execution_helpers import (
    ProbeTool,
    add_task,
    execute_as,
    open_turn,
    registry_of,
)
from tests.event_routing_helpers import mk_session
from tools.catalog import TOOL_CATALOG

WORKER = "unattributed-worker"
TURN = "turn-unattributed"


def _thread(db, thread_id: str) -> None:
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES (?, 'user', 'test/model', 't', 't')",
        (thread_id,),
    )


def _running_worker(db, *, thread_id: str = WORKER, turn_id: str = TURN) -> None:
    """Insert a thread and its running Turn, with no selection of its own."""
    _thread(db, thread_id)
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES (?,?,0,'test/model','running','t','test')""",
        (turn_id, thread_id),
    )
    db.commit()


def _seed_selected_worker(db) -> None:
    """A running worker with a real selection in its own active plan."""
    _running_worker(db)
    plan = service.ensure_plan("thread", WORKER, db=db)
    task = add_task(db, plan.id, "Own task")
    admission.activate(
        task.id, expected_revision=task.revision, actor_id=WORKER,
        provenance="test.unattributed", db=db,
    )
    db.commit()


def _rows(db) -> list:
    return db.execute(
        "SELECT * FROM plan_unattributed_dispatches ORDER BY turn_id",
    ).fetchall()


def _flush(db, thread_id: str, turn_id: str) -> None:
    with service._write(db):
        tool_dispatches.settle_turn(turn_id, db=db)


def test_n_unattributed_calls_produce_one_row_with_count_n_at_turn_terminal(
    isolated_db,
):
    """Zero rows exist before the flush; exactly one row with count N after."""
    db = get_db()
    try:
        _running_worker(db)

        for index in range(3):
            recorded = tool_dispatches.record(
                WORKER, TURN, "bash", dispatch_id=f"call-{index}",
            )
            assert recorded is False

        assert _rows(db) == []

        _flush(db, WORKER, TURN)

        rows = _rows(db)
        assert len(rows) == 1
        assert rows[0]["turn_id"] == TURN
        assert rows[0]["thread_id"] == WORKER
        assert rows[0]["call_count"] == 3
        assert rows[0]["plan_state"] == "none"
    finally:
        db.close()


def test_the_real_turn_ending_writes_the_count(isolated_db):
    """The production path: a Turn that ends writes its own count, with no caller to remember it."""
    from infra.thread_driver import finish_turn

    db = get_db()
    try:
        _running_worker(db)
        for index in range(2):
            tool_dispatches.record(WORKER, TURN, "bash", dispatch_id=f"end-{index}")

        finish_turn(
            TURN, outcome="completed", ended_at="t2",
            successor_turn_id="turn-after", boot_epoch="test", db=db,
        )

        rows = _rows(db)
        assert [(r["turn_id"], r["call_count"]) for r in rows] == [(TURN, 2)]
    finally:
        db.close()


def test_a_turn_with_a_valid_selection_produces_no_row(isolated_db):
    """Every call is attributed, so nothing is left to flush."""
    db = get_db()
    try:
        _seed_selected_worker(db)

        recorded = tool_dispatches.record(WORKER, TURN, "bash", dispatch_id="call-0")
        assert recorded is True

        _flush(db, WORKER, TURN)

        assert _rows(db) == []
    finally:
        db.close()


#: Plans' own tools, exactly as the tool catalog classifies them.
PLANS_CATALOG_TOOLS = tuple(
    sorted(name for name, entry in TOOL_CATALOG.items() if entry.category == "plans")
)


@pytest.mark.asyncio
async def test_plans_category_tools_are_not_counted(isolated_db):
    """A catalog ``plans`` tool never reaches ``record``, so it is never counted."""
    assert PLANS_CATALOG_TOOLS, "the catalog must classify at least one plans tool"
    thread_id = mk_session("unattributed-plans-tool")
    connection = get_db()
    identity = open_turn(connection, thread_id)
    name = PLANS_CATALOG_TOOLS[0]
    plans_tool = ProbeTool(name, result=f"{name} ran")
    registry = registry_of(plans_tool)

    assert await execute_as(registry, identity, thread_id, name) == f"{name} ran"

    _flush(connection, thread_id, identity.turn_id)
    assert _rows(connection) == []
    connection.close()


def test_the_no_selection_path_still_takes_no_write_lock_and_writes_nothing(
    isolated_db,
):
    """Counting in memory adds no SQL write per call, matching the documented contract."""
    db = get_db()
    try:
        _running_worker(db)

        statements: list[str] = []
        db.set_trace_callback(statements.append)
        try:
            recorded = tool_dispatches.record(
                WORKER, TURN, "bash", dispatch_id="call-locked-check",
            )
        finally:
            db.set_trace_callback(None)

        assert recorded is False
        assert not any(
            statement.lstrip().upper().startswith("BEGIN IMMEDIATE")
            for statement in statements
        )
        assert not any(
            statement.lstrip().upper().startswith(
                ("INSERT", "UPDATE", "DELETE"),
            )
            for statement in statements
        )
    finally:
        db.close()


def test_a_flush_failure_does_not_raise_into_the_caller(isolated_db, monkeypatch):
    """A broken plan-state lookup is logged and swallowed, not propagated."""
    db = get_db()
    try:
        _running_worker(db)
        tool_dispatches.record(WORKER, TURN, "bash", dispatch_id="call-0")

        def _broken(*_args, **_kwargs):
            raise RuntimeError("plan state lookup unavailable")

        monkeypatch.setattr(tool_dispatches, "_thread_plan_state", _broken)

        _flush(db, WORKER, TURN)

        assert _rows(db) == []
    finally:
        db.close()


def test_a_call_for_a_turn_that_is_not_live_is_never_counted(isolated_db):
    """No Turn terminal will ever flush it, so counting it would only leak."""
    db = get_db()
    try:
        _thread(db, WORKER)
        db.commit()
    finally:
        db.close()

    assert tool_dispatches.record(WORKER, "turn-already-gone", "probe", dispatch_id="d-gone") is False

    assert "turn-already-gone" not in tool_dispatches._UNATTRIBUTED_COUNTS
