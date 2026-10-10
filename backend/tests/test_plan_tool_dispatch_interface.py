"""The dispatch recorder and per-call settlement at their own interface.

`record` writes a row only for a Turn that is live for the calling thread and a
thread that holds a valid selection in its own plan. A refusal for a Turn that
is not live, or a thread with no valid selection, returns False without taking
the database write lock. Turn admission owns the initial selection;
`test_plan_tool_dispatch_no_selection.py` covers a call that finds none.
`settle` validates its outcome, and a row that is absent or already settled -
for example closed by Turn terminal before the registry's `finally` runs - is
a no-op that also takes no write lock. The last tests reach the same rules
through the registry.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from infra import thread_driver
from infra.database import get_db
from plans import tool_dispatches
from tests.event_routing_helpers import mk_session
from tests.plan_execution_helpers import (
    BlockingProbe,
    ProbeTool,
    abandon_blocking_call,
    dispatch_rows,
    execute_as,
    finish_blocking_call,
    finish_turn,
    open_attempt_ids,
    open_turn,
    registry_of,
    selected_plan,
    start_blocking_call,
)

#: How long a no-op may take while another connection holds the write lock. It
#: stays below the pooled 5s busy timeout, so a call that tries to take the lock
#: fails here as a timeout instead of as a later "database is locked" error.
_NO_LOCK_DEADLINE_S = 4.0


def _under_foreign_write_lock(db_path, fn, *args, **kwargs):
    """Run ``fn`` on a worker thread while another connection holds the write lock.

    Returns ``fn``'s result; raises TimeoutError if ``fn`` blocked on the lock.
    The pooled connection is opened first so the file is already in the WAL
    mode every production connection uses; switching modes needs the lock.
    """
    get_db().execute("SELECT 1").fetchone()
    holder = sqlite3.connect(str(db_path), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(fn, *args, **kwargs).result(timeout=_NO_LOCK_DEADLINE_S)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        pool.shutdown(wait=True)


def _selected_executor(db, name: str):
    """Return a thread with one valid selection and that selection's attempt id."""
    thread_id = mk_session(name)
    _plan, selected = selected_plan(db, thread_id)
    return thread_id, selected["admission"]["selection"]["attempt_id"]


def _refused_turn(db, case: str, thread_id: str) -> str | None:
    """Return a Turn id that `record` must refuse for ``thread_id``."""
    if case == "no_turn_identity":
        return None
    if case == "turn_of_another_thread":
        other = mk_session(f"interface-other-{thread_id}")
        return open_turn(db, other, "other-thread-turn").turn_id
    turn_id = open_turn(db, thread_id, "completed-turn").turn_id
    finish_turn(db, turn_id)
    return turn_id


@pytest.mark.parametrize(
    "case", ["no_turn_identity", "turn_of_another_thread", "completed_turn"],
)
def test_record_refuses_a_turn_that_is_not_live_for_the_thread(isolated_db, case):
    """False, no row, and no write lock."""
    db = get_db()
    thread_id, attempt_id = _selected_executor(db, f"interface-refused-{case}")
    turn_id = _refused_turn(db, case, thread_id)

    recorded = _under_foreign_write_lock(
        isolated_db, tool_dispatches.record, thread_id, turn_id, "probe",
        dispatch_id=f"refused-{case}",
    )

    assert recorded is False
    assert dispatch_rows(db) == []
    assert open_attempt_ids(db) == {attempt_id}
    db.close()


def test_record_refuses_a_thread_without_a_valid_selection(isolated_db):
    """A live Turn alone is not a selection; no write lock."""
    thread_id = mk_session("interface-no-selection")
    db = get_db()
    identity = open_turn(db, thread_id)

    recorded = _under_foreign_write_lock(
        isolated_db, tool_dispatches.record, thread_id, identity.turn_id, "probe",
        dispatch_id="no-selection",
    )

    assert recorded is False
    assert dispatch_rows(db) == []
    db.close()


def _seed_dispatch(db, attempt_id: str, turn_id: str, dispatch_id: str, *, settled: bool):
    """Insert one call row, open or already closed by Turn terminal."""
    db.execute(
        """INSERT INTO plan_tool_dispatches
           (id,attempt_id,turn_id,tool_name,started_at,settled_at,outcome)
           VALUES (?,?,?,'probe','t-start',?,?)""",
        (
            dispatch_id, attempt_id, turn_id,
            "t-terminal" if settled else None, "unknown" if settled else None,
        ),
    )
    db.commit()


def _dispatch_state(db, dispatch_id: str) -> tuple:
    row = db.execute(
        "SELECT settled_at,outcome FROM plan_tool_dispatches WHERE id=?", (dispatch_id,),
    ).fetchone()
    return tuple(row)


@pytest.mark.parametrize("outcome", ["cancelled", "", "SUCCEEDED"])
def test_settle_rejects_an_outcome_outside_the_contract(isolated_db, outcome):
    """Only succeeded/refused/failed/unknown are outcomes."""
    db = get_db()
    thread_id, attempt_id = _selected_executor(db, f"interface-outcome-{outcome or 'empty'}")
    turn_id = open_turn(db, thread_id).turn_id
    _seed_dispatch(db, attempt_id, turn_id, "open-call", settled=False)

    with pytest.raises(ValueError):
        tool_dispatches.settle("open-call", outcome)

    assert _dispatch_state(db, "open-call") == (None, None)
    db.close()


def test_settle_of_an_absent_row_is_a_no_op_without_a_write_lock(isolated_db):
    """Reading first finds nothing, so nothing is locked."""
    assert _under_foreign_write_lock(
        isolated_db, tool_dispatches.settle, "never-recorded", "succeeded",
    ) is False
    assert dispatch_rows(get_db()) == []


def test_settle_of_an_already_settled_row_is_a_no_op_without_a_write_lock(isolated_db):
    """Turn terminal's `unknown` is never overwritten."""
    db = get_db()
    thread_id, attempt_id = _selected_executor(db, "interface-already-settled")
    turn_id = open_turn(db, thread_id).turn_id
    _seed_dispatch(db, attempt_id, turn_id, "closed-call", settled=True)

    assert _under_foreign_write_lock(
        isolated_db, tool_dispatches.settle, "closed-call", "succeeded",
    ) is False

    assert _dispatch_state(db, "closed-call") == ("t-terminal", "unknown")
    assert open_attempt_ids(db) == {attempt_id}
    db.close()


@pytest.mark.asyncio
async def test_a_call_returning_after_turn_terminal_keeps_the_terminal_settlement(
    isolated_db,
):
    """The registry's late settlement is a no-op.

    The Turn ends while a recorded call is still running, so Turn terminal
    closes the row as `unknown`. When the call later returns, its result is
    returned unchanged and the terminal's settlement is kept.
    """
    thread_id = mk_session("interface-late-return")
    db = get_db()
    identity = open_turn(db, thread_id, "late-turn")
    selected_plan(db, thread_id)
    probe = BlockingProbe(result="late result")
    call = await start_blocking_call(registry_of(probe), identity, thread_id, probe)
    try:
        thread_driver.finish_turn(
            "late-turn", outcome="error", ended_at="t-terminal",
            successor_turn_id="late-successor", boot_epoch="boot-2",
            error="backend restarted", db=db,
        )
        [closed] = dispatch_rows(db)
        terminal_settlement = (closed["settled_at"], closed["outcome"])
        assert terminal_settlement[0] is not None
        assert terminal_settlement[1] == "unknown"

        assert await finish_blocking_call(probe, call) == "late result"
    finally:
        await abandon_blocking_call(probe, call)

    [kept] = dispatch_rows(db)
    assert (kept["settled_at"], kept["outcome"]) == terminal_settlement
    db.close()

