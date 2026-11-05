"""Pipe failures cannot erase possibly delivered input from live occupancy."""

import asyncio
import json
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("admitted_writes")  # write accounting, not account binding, is under test


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["claude-code", "codex"])
@pytest.mark.parametrize("failure,expected", [("write", {}), ("drain", {"input": 20})])
async def test_write_failure_accounting_distinguishes_no_write_from_ambiguous(
    harness, failure, expected,
):
    from subagents.cc_stream import CCStreamSession
    from subagents.codex_app_server import CodexAppServerSession

    def write(_payload):
        if failure == "write":
            raise BrokenPipeError("before pipe acceptance")

    async def drain():
        raise BrokenPipeError("after pipe write")

    process = SimpleNamespace(returncode=None, stdin=SimpleNamespace(write=write, drain=drain))
    if harness == "claude-code":
        session = CCStreamSession(binary="unused", session_id="fixture")
        send = session.feed
    else:
        session = CodexAppServerSession(binary="unused", task=None, codex_thread_id="thread")
        session._track_active_turn("turn")
        send = session.steer
    session._proc = process
    with pytest.raises(BrokenPipeError):
        await send("input", input_context=("input", 20))
    assert session.tool_context.pending_inputs == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected", [False, True])
async def test_codex_rpc_rejection_releases_count_but_lost_ack_preserves_it(monkeypatch, rejected):
    from unittest.mock import Mock
    from subagents.codex_app_server import CodexAppServerSession, CodexAppServerError

    session = CodexAppServerSession(binary="unused", task=None, codex_thread_id="thread")
    session._track_active_turn("turn")
    monkeypatch.setattr("subagents.codex_app_server.STEER_REQUEST_TIMEOUT", 0.01)

    def write(payload):
        request = json.loads(payload)
        if rejected:
            asyncio.get_running_loop().call_soon(session._handle_response, {
                "id": request["id"], "error": {"code": -32602, "message": "rejected"},
            })

    async def drain():
        return None

    session._proc = SimpleNamespace(returncode=None, stdin=SimpleNamespace(write=write, drain=drain), kill=Mock())
    with pytest.raises(CodexAppServerError):
        await session.steer("input", input_context=("input", 20))
    assert session.tool_context.pending_inputs == ({} if rejected else {"input": 20})
    assert session._pending == {}
    session._proc.kill.assert_not_called()


@pytest.mark.asyncio
async def test_codex_stale_thread_ack_and_receipt_cannot_retire_current_input():
    from core.fork_codex_context import CodexForkContext
    from subagents.codex_app_server import CodexAppServerSession

    session = CodexAppServerSession(binary="unused", task=None, codex_thread_id="thread")
    session._track_active_turn("turn")
    session.tool_context = CodexForkContext(None, 100)
    session.tool_context.pending_inputs["input"] = 20
    for thread, turn in [("other", "turn"), ("thread", "old-turn")]:
        await session._handle_notification("item/started", {
            "threadId": thread, "turnId": turn,
            "item": {"type": "userMessage", "id": "user", "clientId": "input"},
        })
        await session._handle_notification("thread/tokenUsage/updated", {
            "threadId": thread, "turnId": turn,
            "tokenUsage": {"last": {"inputTokens": 10, "outputTokens": 1},
                           "total": {"inputTokens": 10, "outputTokens": 1}},
        })
    assert session.tool_context.pending_inputs == {"input": 20}
    assert session.tool_context.consumed_inputs == set()
    assert session.tool_context.tokens == 120


@pytest.mark.asyncio
async def test_codex_partial_current_usage_preserves_floor_and_natural_exit():
    from core.fork_codex_context import CodexForkContext
    from subagents.codex_app_server import CodexAppServerSession

    exits = []
    kills = []
    async def on_exit(code, detail):
        exits.append((code, detail))

    session = CodexAppServerSession(binary="unused", task=None, codex_thread_id="thread", on_exit=on_exit)
    session._track_active_turn("turn")
    session.tool_context = CodexForkContext(None, 250)
    stream = asyncio.StreamReader()
    stream.feed_data((json.dumps({"method": "thread/tokenUsage/updated", "params": {
        "threadId": "thread", "turnId": "turn",
        "tokenUsage": {"last": {"inputTokens": 100}, "total": {"inputTokens": 100}},
    }}) + "\n").encode())
    stream.feed_eof()
    process = SimpleNamespace(returncode=None, stdout=stream)
    def kill():
        kills.append(True)
        process.returncode = -9
    process.kill = kill
    async def wait():
        if process.returncode is None:
            process.returncode = 0
        return process.returncode
    process.wait = wait
    session._proc = process
    await session._read_loop()
    assert exits == [(0, "")]
    assert kills == []
    assert session.tool_context.tokens == 250
    assert session._usage_by_turn["turn"]["last"]["inputTokens"] == 100
    assert not session._closing  # Intentional-close must not suppress the terminal owner.
