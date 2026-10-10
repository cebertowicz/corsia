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
