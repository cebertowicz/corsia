"""Dead launcher observation must not race the reader's family cleanup."""

import asyncio

import pytest

from subagents import codex_runtime
from subagents.codex_app_server import CodexAppServerSession
from subagents.codex_inbox_batch import deliver_live_inbox_batch
from test_codex_inbox_steer import (
    _clear_live, _enqueue, _input_row, _install_live, _seed_live_turn,
)


@pytest.mark.asyncio
async def test_inbox_preserves_reader_exit_owner_and_queued_successor(isolated_db, monkeypatch):
    from core import turn_core
    from infra.database import get_db

    thread_id = "codex-reader-cleanup-owner"
    identity = _seed_live_turn(thread_id)
    receipt = _enqueue(thread_id)
    cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event()
    settlements = []

    class Stdout:
        async def readline(self):
            return b""

    class Process:
        returncode = 7
        stdout = Stdout()

    session = CodexAppServerSession(binary="codex", task=None)
    live = _install_live(thread_id, identity, session)
    codex_runtime._APP_SESSIONS[thread_id] = session
    session._proc = Process()

    async def wait_exit(_timeout):
        cleanup_entered.set()
        await cleanup_release.wait()

    async def on_exit(code, error):
        await codex_runtime._finish_transport_exit(
            thread_id, live, session, code, error, lambda *_args: None,
        )

    async def drive(settlement):
        settlements.append(settlement)
        return settlement.result

    monkeypatch.setattr(session, "_wait_process_exit", wait_exit)
    monkeypatch.setattr(turn_core, "drive_turn_settlement", drive)
    monkeypatch.setattr("infra.state.broadcast_to_thread", lambda *_args: None)
    session.set_callbacks(on_exit=on_exit)
    session._reader = asyncio.create_task(session._read_loop())
    try:
        await asyncio.wait_for(cleanup_entered.wait(), 1)
        assert await deliver_live_inbox_batch(thread_id) is False
        assert settlements == []
        assert codex_runtime._LIVE.get(thread_id) is live
        row = _input_row(receipt.message_id)
        assert row["delivery_state"] == "queued"
        assert row["active_turn_id"] is None
        with get_db() as db:
            assert db.execute(
                "SELECT status FROM active_turns WHERE id=?", (identity.turn_id,),
            ).fetchone()[0] == "running"

        cleanup_release.set()
        await asyncio.wait_for(session._reader, 1)
        assert len(settlements) == 1
        assert settlements[0].successor is not None
        assert _input_row(receipt.message_id)["active_turn_id"] == settlements[0].successor.turn_id
        assert codex_runtime._LIVE.get(thread_id) is None
    finally:
        cleanup_release.set()
        await asyncio.wait_for(session._reader, 1)
        _clear_live(thread_id)


def _fenced_successor(thread_id: str):
    """Claim one input, fence it, then queue the successor it must unblock."""
    from core.turn_core import BOOT_EPOCH
    from infra.database import get_db
    from infra.input_writes import begin_batch_write
    from infra.thread_driver import admit_input

    with get_db() as db:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES (?, ?, 'codex/gpt-5-codex', 't0', 't0')",
            (thread_id, "u-test"),
        )
        db.commit()
        admission = admit_input(
            thread_id,
            role="system",
            content="fenced successor input",
            intent="apply_now",
            message_id=f"{thread_id}-fenced",
            turn_id=f"{thread_id}-turn",
            boot_epoch=BOOT_EPOCH,
            admitted_at="t1",
            db=db,
        )
        assert admission.batch is not None
        begin_batch_write(
            admission.batch,
            attempt_id=f"{thread_id}-attempt",
            started_at="t2",
            db=db,
        )
    return admission, _enqueue(thread_id, message_id=f"{thread_id}-next")


@pytest.mark.asyncio
async def test_transport_exit_quarantines_fenced_input_and_drives_successor(
    isolated_db, monkeypatch,
) -> None:
    """A process exit makes a fenced write ambiguous and drives the next input."""
    from infra.database import get_db

    thread_id = "codex-fenced-exit"
    admission, next_input = _fenced_successor(thread_id)
    driven = []

    async def drive(admission):
        driven.append(admission)

    monkeypatch.setattr("infra.thread_execution.drive_turn_admission", drive)
    session = CodexAppServerSession(binary="codex", task=None)
    live = _install_live(thread_id, admission.batch.identity, session)
    codex_runtime._APP_SESSIONS[thread_id] = session
    try:
        await codex_runtime._finish_transport_exit(
            thread_id, live, session, 1, "provider exited", lambda *_args: None,
        )
        assert codex_runtime._LIVE.get(thread_id) is None
    finally:
        _clear_live(thread_id)

    with get_db() as db:
        assert db.execute(
            "SELECT status FROM active_turns WHERE id=?", (admission.batch.turn_id,),
        ).fetchone()[0] == "error"
        fenced = _input_row(f"{thread_id}-fenced")
        assert (fenced["write_outcome"], fenced["delivery_state"]) == (
            "ambiguous", "ambiguous",
        )
        assert len(driven) == 1
        assert [item.message_id for item in driven[0].items] == [next_input.message_id]
        assert _input_row(next_input.message_id)["active_turn_id"] == driven[0].turn_id


@pytest.mark.asyncio
async def test_rejected_input_quarantines_fenced_input_and_drives_successor(
    isolated_db, monkeypatch,
) -> None:
    """The non-deferred rejection path keeps synchronous successor driving."""
    from infra.database import get_db

    thread_id = "codex-fenced-rejection"
    admission, next_input = _fenced_successor(thread_id)
    driven = []

    async def drive(admission):
        driven.append(admission)

    monkeypatch.setattr("infra.thread_execution.drive_turn_admission", drive)
    session = CodexAppServerSession(binary="codex", task=None)
    live = _install_live(thread_id, admission.batch.identity, session)
    try:
        await codex_runtime._finish_rejected_input(
            thread_id, live, admission.batch.identity, "provider rejected", lambda *_args: None,
        )
    finally:
        _clear_live(thread_id)

    with get_db() as db:
        assert db.execute(
            "SELECT status FROM active_turns WHERE id=?", (admission.batch.turn_id,),
        ).fetchone()[0] == "error"
        fenced = _input_row(f"{thread_id}-fenced")
        assert (fenced["write_outcome"], fenced["delivery_state"]) == (
            "ambiguous", "ambiguous",
        )
        assert len(driven) == 1
        assert [item.message_id for item in driven[0].items] == [next_input.message_id]
        assert _input_row(next_input.message_id)["active_turn_id"] == driven[0].turn_id
