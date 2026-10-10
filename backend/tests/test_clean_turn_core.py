"""Harness-facing Turn API contracts over the clean state owner."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest


def _db() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','native/test','t0','t0')"
    )
    return db


def test_active_turn_thread_ids_returns_nonterminal_thread_ids() -> None:
    """The active-Turn index projects its owning CORSIA thread ids."""
    from core.turn_core import active_turn_thread_ids
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn

    db = _db()
    try:
        accept_thread_input(
            "thread-1", role="user", content="hello", intent="apply_now",
            message_id="input-1", db=db,
        )
        db.commit()
        assert start_idle_turn(
            "thread-1", turn_id="turn-1", boot_epoch="boot-1",
            started_at="t1", db=db,
        ) is not None
        assert active_turn_thread_ids(db=db) == {"thread-1"}
    finally:
        db.close()


def test_harness_start_rejects_a_missing_thread() -> None:
    """A harness cannot bind a Turn to a nonexistent thread."""
    from core.turn_core import TurnThreadMissingError, persist_turn_start

    db = _db()
    try:
        with pytest.raises(
            TurnThreadMissingError,
            match="turn thread does not exist: missing-thread",
        ):
            persist_turn_start(
                "turn-1", "missing-thread", "native/test", "hello", db=db,
            )
    finally:
        db.close()


def test_harness_start_adopts_the_exact_preadmitted_turn() -> None:
    """A harness binds the Turn already created with its claimed input."""
    from core.turn_core import persist_turn_start
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn

    db = _db()
    try:
        accept_thread_input(
            "thread-1", role="user", content="hello", intent="apply_now",
            message_id="input-1", db=db,
        )
        db.commit()
        batch = start_idle_turn(
            "thread-1", turn_id="turn-1", boot_epoch="boot-1",
            started_at="t1", db=db,
        )
        assert batch is not None
        identity = persist_turn_start(
            "turn-1", "thread-1", "native/test", "hello", db=db,
        )
        assert identity == batch.identity
        assert db.execute(
            "SELECT COUNT(*) FROM active_turns"
        ).fetchone()[0] == 1
    finally:
        db.close()


def test_harness_terminal_uses_the_clean_compare_and_set() -> None:
    """Every harness terminal reaches the same first-writer-wins transition."""
    from core.turn_core import persist_turn_terminal
    from infra.thread_driver import TerminalResult
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn

    db = _db()
    try:
        accept_thread_input(
            "thread-1", role="user", content="hello", intent="apply_now",
            message_id="input-1", db=db,
        )
        db.commit()
        assert start_idle_turn(
            "thread-1", turn_id="turn-1", boot_epoch="boot-1",
            started_at="t1", db=db,
        ) is not None
        result = persist_turn_terminal(
            "turn-1", "cancelled", db=db, ended_at="t2",
        )
        assert result is TerminalResult.APPLIED
        assert persist_turn_terminal(
            "turn-1", "cancelled", db=db, ended_at="t3",
        ) is TerminalResult.ALREADY_SAME_OUTCOME
    finally:
        db.close()


@pytest.mark.asyncio
async def test_async_terminal_drives_the_committed_successor(
    isolated_db, monkeypatch,
) -> None:
    """A harness terminal does not leave its already-claimed successor inert."""
    from infra.database import get_db
    import infra.thread_execution as execution
    from core.turn_core import settle_turn_terminal
    from infra.thread_driver import TerminalResult
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','native/test','t0','t0')"
    )
    accept_thread_input(
        "thread-1", role="user", content="first", intent="apply_now",
        message_id="input-1", db=db,
    )
    db.commit()
    first = start_idle_turn(
        "thread-1", turn_id="turn-1", boot_epoch="boot-1",
        started_at="t1", db=db,
    )
    assert first is not None
    from infra.input_writes import begin_batch_write, settle_batch_write

    begin_batch_write(first, attempt_id="attempt-1", started_at="t1", db=db)
    settle_batch_write("attempt-1", outcome="accepted", db=db)
    accept_thread_input(
        "thread-1", role="user", content="next", intent="queue_after_turn",
        message_id="input-2", db=db,
    )
    db.commit()
    db.close()

    driven = []

    async def _drive(settlement):
        driven.append(settlement.successor)

    monkeypatch.setattr(execution, "drive_terminal_settlement", _drive)
    result = await settle_turn_terminal(
        "turn-1", "completed", ended_at="t2",
    )
    assert result is TerminalResult.APPLIED
    assert len(driven) == 1
    assert driven[0] is not None and driven[0].items[0].message_id == "input-2"


@pytest.mark.asyncio
async def test_cancelled_harness_bind_drives_the_committed_successor(
    isolated_db, monkeypatch,
) -> None:
    """Cancellation during harness binding cannot strand the next FIFO head."""
    import core.turn_core as turn_core
    import infra.thread_execution as execution
    from infra.database import get_db
    from infra.thread_driver import admit_input
    from infra.input_admission import accept_thread_input

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','native/test','t0','t0')"
    )
    db.commit()
    admission = admit_input(
        "thread-1", role="user", content="first", intent="apply_now",
        message_id="input-1", turn_id="turn-1", boot_epoch="boot-1",
        admitted_at="t1", db=db,
    )
    assert admission.batch is not None
    accept_thread_input(
        "thread-1", role="user", content="next", intent="queue_after_turn",
        message_id="input-2", db=db,
    )
    db.commit()
    db.close()

    start_entered = asyncio.Event()
    release_start = asyncio.Event()
    real_run_in_db = turn_core.run_in_db

    async def delayed_run(function, *args, **kwargs):
        if function is turn_core.persist_turn_start:
            start_entered.set()
            await release_start.wait()
        return await real_run_in_db(function, *args, **kwargs)

    driven = []

    async def drive(settlement):
        driven.append(settlement.successor)

    monkeypatch.setattr(turn_core, "run_in_db", delayed_run)
    monkeypatch.setattr(execution, "drive_terminal_settlement", drive)

    bound = []
    task = asyncio.create_task(turn_core.persist_turn_start_bound(
        "turn-1", "thread-1", "native/test", "first", bound.append,
    ))
    await start_entered.wait()
    task.cancel()
    release_start.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert bound and bound[0].turn_id == "turn-1"
    assert len(driven) == 1
    assert driven[0] is not None and driven[0].items[0].message_id == "input-2"
