"""A missing current call receipt must terminate without running a tool."""

import asyncio
import time

import pytest

from test_codex_durable_receipts import group, lines
from test_codex_tool_control_lifecycle import Process, managed_session, tool_call
from tests.contract.receipt_budget_scope import scoped_receipt_budget
from codex_runtime_fixtures import declare_codex_tool_step


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", ["missing", "old-turn", "other-call"])
async def test_missing_current_receipt_reaches_failure_owner(tmp_path, monkeypatch, receipt):
    from subagents import codex_tool_requests

    monkeypatch.setattr(codex_tool_requests, "TOOL_RECEIPT_TIMEOUT_SECONDS", 0.05, raising=False)
    proc, exits, executed = Process(), [], []

    async def on_exit(code, error):
        exits.append((code, error))

    async def execute(*_args, **_kwargs):
        executed.append(True)
        return "unexpected tool execution"

    monkeypatch.setattr("subagents.harness_tool_bridge.execute_tool", execute)
    session = managed_session(proc, on_exit=on_exit)
    path = tmp_path / "rollout.jsonl"
    path.touch()
    session._tool_requests.rollout_path = str(path)
    await session._tool_requests.prepare_turn()
    if receipt != "missing":
        path.write_bytes(lines(*group(
            turn="old" if receipt == "old-turn" else "turn",
            calls=("call-turn",) if receipt == "old-turn" else ("other",),
        )))
    session._reader = asyncio.create_task(session._read_loop())
    proc.send(tool_call())
    try:
        await asyncio.wait_for(asyncio.shield(session._reader), 1)
        assert len(exits) == 1
        assert exits[0][0] == -9
        assert "Timed out waiting for the current Codex tool usage receipt" in exits[0][1]
        assert not executed and not proc.writes
        assert not session._tool_requests.tasks
        assert session._dynamic_tool_requests_idle.is_set()
    finally:
        await session.terminate(timeout=0.2)


@pytest.mark.asyncio
async def test_receipt_failure_revokes_queued_callback(tmp_path, monkeypatch):
    from subagents import codex_tool_requests

    monkeypatch.setattr(codex_tool_requests, "TOOL_RECEIPT_TIMEOUT_SECONDS", 0.05)
    proc, executed = Process(), []

    async def execute(*_args, **_kwargs):
        executed.append(True)
        return "unexpected"

    monkeypatch.setattr("subagents.harness_tool_bridge.execute_tool", execute)
    session = managed_session(proc)
    path = tmp_path / "rollout.jsonl"
    path.touch()
    session._tool_requests.rollout_path = str(path)
    await session._tool_requests.prepare_turn()
    path.write_bytes(lines(*group(calls=("second",))))
    second = tool_call(rpc=901)
    second["params"]["callId"] = "second"
    try:
        await session._handle_message(tool_call())
        await session._handle_message(second)
        await asyncio.wait_for(session.wait_dynamic_tool_requests_idle(), 1)
        assert proc.returncode == -9
        assert not session._dynamic_tool_admission_open
        assert not executed and not proc.writes
    finally:
        await session.terminate(timeout=0.2)


@pytest.mark.asyncio
async def test_temporarily_drained_queued_callback_is_rejected_and_session_recovers(tmp_path, monkeypatch):
    proc, executed = Process(), []
    session = managed_session(proc)
    declare_codex_tool_step(session, "call-turn", persist=False)
    path = tmp_path / "rollout.jsonl"
    path.touch()
    session._tool_requests.rollout_path = str(path)
    await session._tool_requests.prepare_turn()

    async def execute(*_args, **_kwargs):
        executed.append(True)
        return "finished"

    monkeypatch.setattr("subagents.harness_tool_bridge.execute_tool", execute)
    try:
        async with session._tool_requests.lock:
            await session._handle_message(tool_call())
            session.revoke_dynamic_tool_admission()
        await asyncio.wait_for(session.wait_dynamic_tool_requests_idle(), 1)
        assert len(proc.writes) == 1
        assert proc.writes[0]["id"] == 900
        assert proc.writes[0]["result"]["success"] is False
        assert not executed
        assert proc.returncode is None

        session.restore_dynamic_tool_admission()
        path.write_bytes(lines(*group(calls=("call-turn",))))
        await session._handle_message(tool_call(rpc=901))
        await asyncio.wait_for(session.wait_dynamic_tool_requests_idle(), 1)
        assert proc.writes[1]["id"] == 901
        assert proc.writes[1]["result"]["success"] is True
        assert executed == [True]
    finally:
        await session.terminate(timeout=0.2)


@pytest.mark.asyncio
async def test_receipt_deadline_does_not_limit_tool_execution(tmp_path, monkeypatch):
    from subagents import codex_tool_requests

    monkeypatch.setattr(codex_tool_requests, "TOOL_RECEIPT_TIMEOUT_SECONDS", 0.05)
    proc = Process()
    session = managed_session(proc)
    declare_codex_tool_step(session, "call-turn", persist=False)
    path = tmp_path / "rollout.jsonl"
    path.touch()
    session._tool_requests.rollout_path = str(path)
    await session._tool_requests.prepare_turn()
    path.write_bytes(lines(*group(calls=("call-turn",))))
    entered, release = asyncio.Event(), asyncio.Event()

    async def execute(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return "finished"

    monkeypatch.setattr("subagents.harness_tool_bridge.execute_tool", execute)
    try:
        await session._handle_message(tool_call())
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(0.1)
        assert proc.returncode is None and not proc.writes
        release.set()
        await asyncio.wait_for(session.wait_dynamic_tool_requests_idle(), 1)
        assert proc.writes[0]["result"]["success"] is True
    finally:
        release.set()
        await session.terminate(timeout=0.2)


@pytest.mark.asyncio
async def test_scoped_receipt_budget_leaves_other_calls_at_the_full_budget(tmp_path, monkeypatch):
    """A short budget scoped to one stale call must not shorten a different,
    genuinely delayed-but-real receipt wait in the same process (#2720)."""
    from subagents import codex_tool_requests

    monkeypatch.setattr(codex_tool_requests, "TOOL_RECEIPT_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(
        codex_tool_requests.ToolRequests, "_usage",
        scoped_receipt_budget({"call-stale"}, stale_budget_seconds=0.1),
    )
    proc, executed = Process(), []

    async def execute(*_args, **_kwargs):
        executed.append(True)
        return "finished"

    monkeypatch.setattr("subagents.harness_tool_bridge.execute_tool", execute)
    session = managed_session(proc)
    declare_codex_tool_step(session, "call-turn", persist=False)
    path = tmp_path / "rollout.jsonl"
    path.touch()
    session._tool_requests.rollout_path = str(path)
    await session._tool_requests.prepare_turn()
    try:
        await session._handle_message(tool_call())  # callId "call-turn", not the stale one.
        # Above the 0.1s stale budget, well below the 5.0s budget this test set.
        await asyncio.sleep(0.3)
        assert not executed and proc.returncode is None, (
            "a non-stale call must not be held to the short stale-receipt budget"
        )
        path.write_bytes(lines(*group(calls=("call-turn",))))
        await asyncio.wait_for(session.wait_dynamic_tool_requests_idle(), 2)
        assert executed == [True]
        assert proc.writes[0]["result"]["success"] is True
    finally:
        await session.terminate(timeout=0.2)


@pytest.mark.asyncio
async def test_scoped_receipt_budget_still_fails_the_named_stale_call_fast(tmp_path, monkeypatch):
    """The named stale call keeps failing fast at the short budget (#2720)."""
    from subagents import codex_tool_requests

    monkeypatch.setattr(codex_tool_requests, "TOOL_RECEIPT_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(
        codex_tool_requests.ToolRequests, "_usage",
        scoped_receipt_budget({"call-turn"}, stale_budget_seconds=0.1),
    )
    proc, exits = Process(), []

    async def on_exit(code, error):
        exits.append((code, error))

    session = managed_session(proc, on_exit=on_exit)
    path = tmp_path / "rollout.jsonl"
    path.touch()  # No matching receipt is ever written for "call-turn".
    session._tool_requests.rollout_path = str(path)
    await session._tool_requests.prepare_turn()
    session._reader = asyncio.create_task(session._read_loop())
    proc.send(tool_call())
    started = time.monotonic()
    try:
        await asyncio.wait_for(asyncio.shield(session._reader), 2)
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"stale call took {elapsed:.2f}s; must use the short budget"
        assert exits and exits[0][0] == -9
        assert "Timed out waiting for the current Codex tool usage receipt after 0.1s" in exits[0][1]
    finally:
        await session.terminate(timeout=0.2)


@pytest.mark.asyncio
async def test_permanent_receipt_failure_cannot_restore_admission(tmp_path):
    session = managed_session(Process())
    # No cursor is a permanent control failure, not temporary lifecycle drain.
    await session._tool_requests._execute(tool_call())
    session.restore_dynamic_tool_admission()
    assert not session._dynamic_tool_admission_open
    assert session._terminal_error is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("arrives_after_deadline", [False, True])
async def test_delayed_valid_receipt_obeys_terminal_boundary(tmp_path, monkeypatch, arrives_after_deadline):
    from subagents import codex_tool_requests

    monkeypatch.setattr(codex_tool_requests, "TOOL_RECEIPT_TIMEOUT_SECONDS", .4)
    proc, executed = Process(), []
    session = managed_session(proc)
    declare_codex_tool_step(session, "call-turn", persist=False)
    path = tmp_path / "rollout.jsonl"
    path.touch()
    session._tool_requests.rollout_path = str(path)
    await session._tool_requests.prepare_turn()

    async def execute(*_args, **_kwargs):
        executed.append(True)
        return "delayed receipt accepted"

    monkeypatch.setattr("subagents.harness_tool_bridge.execute_tool", execute)
    try:
        await session._handle_message(tool_call())
        if arrives_after_deadline:
            await asyncio.wait_for(session.wait_dynamic_tool_requests_idle(), 1)
        else:
            await asyncio.sleep(.1)
            assert not executed and proc.returncode is None
        path.write_bytes(lines(*group(calls=("call-turn",))))
        await asyncio.wait_for(session.wait_dynamic_tool_requests_idle(), 1)
        if arrives_after_deadline:
            session.restore_dynamic_tool_admission()
            await session._handle_message(tool_call(rpc=901))
            await asyncio.wait_for(session.wait_dynamic_tool_requests_idle(), 1)
            assert not executed and proc.returncode == -9
        else:
            assert executed == [True]
            assert proc.writes[0]["result"]["success"] is True
            assert proc.returncode is None
    finally:
        await session.terminate(timeout=.2)
