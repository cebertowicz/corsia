from __future__ import annotations

import asyncio
import sys

import pytest

from cc_runtime_fixtures import _seed_parent, child_db, settle_test_input  # noqa: F401
from test_cc_runtime_wedge import _admit_turn


@pytest.mark.asyncio
async def test_launch_exit_before_first_event_ends_turn(child_db, monkeypatch):
    from subagents.cc_handle_close import finalize
    from subagents.cc_process_exit import handle_process_exit
    from subagents.cc_runtime_state import _LIVE, _Live
    from subagents.cc_stream import CCStreamSession
    from subagents import vendor_write_admission
    from vendor_account_fixtures import default_home

    thread_id = "cc-failed-launch"
    _LIVE.reset()
    _seed_parent(child_db, sid=thread_id)
    batch = _admit_turn(child_db, thread_id, "cc-failed-launch-turn", model="claude-code/sonnet", text="hello")
    settle_test_input(batch)
    frames = []
    live = _Live(session=None, parent_thread_id=None, name="cc", model="sonnet",
                 corsia_turn=batch.identity, turn_active=True)

    async def on_exit(code, stderr):
        await handle_process_exit(thread_id, live, code, stderr, lambda _, frame: frames.append(frame),
                                  finalize_callback=finalize)

    session = CCStreamSession(binary=sys.executable, session_id="failed", on_exit=on_exit,
                              claude_dir=default_home("claude-code"))  # a launch runs in its account's home
    live.session = session
    _LIVE.put(thread_id, live)
    monkeypatch.setattr(vendor_write_admission, "prepare_session_launch", lambda *_args: None)
    monkeypatch.setattr(session, "build_cmd", lambda: [sys.executable, "-c", "import sys; sys.stderr.write('stub launch failed'); sys.exit(1)"])
    try:
        await session.start()
        await asyncio.wait_for(session._reader, 5)
        row = child_db.execute("SELECT status, error FROM active_turns WHERE id=?", (batch.turn_id,)).fetchone()
        assert row["status"] == "error"
        assert "stub launch failed" in row["error"]
        assert any(frame.get("type") == "turn_complete" for frame in frames)
        assert _LIVE.get(thread_id) is None
    finally:
        _LIVE.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [None, "retirement", "unretired", "publication", "cancelled"],
)
async def test_failed_process_retires_before_driving_queued_successor(
    child_db, monkeypatch, caplog, failure,
):
    from core.turn_core import BOOT_EPOCH
    from infra import thread_execution
    from infra.thread_driver import admit_input
    from infra.time import utc_now_iso
    from subagents.cc_handle_close import finalize
    from subagents import cc_process_exit
    from subagents.cc_process_exit import finish_process_exit, handle_process_exit
    from subagents.cc_runtime_state import _LIVE, _Live
    from subagents.cc_stream import CCStreamSession

    thread_id = "cc-failed-exit-successor"
    _LIVE.reset()
    _seed_parent(child_db, sid=thread_id)
    batch = _admit_turn(child_db, thread_id, "cc-failed-exit-turn", model="claude-code/sonnet", text="hello")
    settle_test_input(batch)
    queued = admit_input(thread_id, role="user", content="next", intent="apply_now",
                         message_id="cc-queued-message", turn_id="cc-queued-turn",
                         boot_epoch=BOOT_EPOCH, admitted_at=utc_now_iso(), db=child_db)
    assert queued.batch is None
    live = _Live(session=None, parent_thread_id=None, name="cc", model="sonnet",
                 corsia_turn=batch.identity, turn_active=True)
    frames = []
    drives = []

    async def drive(admission):
        drives.append(admission)
        assert _LIVE.get(thread_id) is None
        assert not live.event_lock.locked()

    monkeypatch.setattr(thread_execution, "drive_turn_admission", drive)

    def retire(*args, **kwargs):
        if failure == "unretired":
            raise RuntimeError("unretired failed")
        result = finalize(*args, **kwargs)
        if failure == "retirement":
            raise RuntimeError("retirement failed")
        return result

    if failure in {"publication", "cancelled"}:
        async def fail_publication(*_args, **_kwargs):
            if failure == "cancelled":
                raise asyncio.CancelledError()
            raise RuntimeError("publication failed")
        monkeypatch.setattr(cc_process_exit, "_publish_process_exit", fail_publication)

    async def on_exit(code, stderr):
        await handle_process_exit(thread_id, live, code, stderr, lambda _, frame: frames.append(frame),
                                  finalize_callback=retire, finish_callback=finish_process_exit)

    session = CCStreamSession(binary=sys.executable, session_id="failed", on_exit=on_exit)
    live.session = session
    _LIVE.put(thread_id, live)
    try:
        session._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import sys; sys.stderr.write('stub launch failed'); sys.exit(1)",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        session._reader = asyncio.create_task(session._read_loop())
        if failure == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(session._reader, 5)
        elif failure == "unretired":
            # The exited process still owns the runtime: never drive a successor past it.
            with pytest.raises(AssertionError, match="retained runtime"):
                await asyncio.wait_for(session._reader, 5)
            assert drives == []
            assert f"settled turn {batch.turn_id} but kept its exited process" in caplog.text
            return
        elif failure:
            with pytest.raises(RuntimeError, match=f"{failure} failed"):
                await asyncio.wait_for(session._reader, 5)
        else:
            await asyncio.wait_for(session._reader, 5)
        row = child_db.execute("SELECT status, error FROM active_turns WHERE id=?", (batch.turn_id,)).fetchone()
        assert row["status"] == "error"
        assert "stub launch failed" in row["error"]
        assert len(drives) == 1
        if failure:
            assert f"turn {batch.turn_id} failed after settling" in caplog.text
        if failure not in {"publication", "cancelled"}:
            assert any(frame.get("type") == "turn_complete" for frame in frames)
    finally:
        _LIVE.reset()
