"""Durable receipt waits preserve tool revocation and process-exit ownership."""

import asyncio
import json

import pytest

from subagents.codex_app_server import CodexAppServerSession
from codex_runtime_fixtures import declare_codex_tool_step


class Process:
    """A deterministic pipe peer; only usage replies stand in for stock Codex."""

    def __init__(self):
        self.returncode = None
        self.stdout = asyncio.StreamReader()
        self.stdin = self
        self.writes = []

    def send(self, frame):
        self.stdout.feed_data(json.dumps(frame).encode() + b"\n")

    def write(self, payload):
        self.writes.append(json.loads(payload))

    async def drain(self):
        pass

    def kill(self):
        self.returncode = -9
        self.stdout.feed_eof()

    async def wait(self):
        return self.returncode


def tool_call(turn="turn", rpc=900):
    return {"id": rpc, "method": "item/tool/call", "params": {
        "threadId": "provider", "turnId": turn, "callId": f"call-{turn}",
        "tool": "edit", "arguments": {},
    }}


def managed_session(proc, **kwargs):
    session = CodexAppServerSession(
        binary="unused", task=None, codex_thread_id="provider",
        corsia_thread_id="thread", tool_registry=object(), **kwargs,
    )
    session._active_turn_id = "turn"
    session._proc = proc
    return session


@pytest.mark.asyncio
async def test_revoked_managed_tool_is_rejected_without_starting_a_usage_wait():
    proc = Process()
    session = managed_session(proc)
    session.revoke_dynamic_tool_admission()
    try:
        await session._handle_message(tool_call())
        assert len(proc.writes) == 1
        assert proc.writes[0]["id"] == 900
        assert proc.writes[0]["result"]["success"] is False
        assert session._dynamic_tool_requests_idle.is_set()
    finally:
        await session._tool_requests.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("publication_fails", (False, True))
async def test_usage_failure_uses_the_existing_process_exit_owner(monkeypatch, publication_fails):
    exited = []

    events = []

    async def publication(frame):
        events.append(frame)
        if publication_fails:
            raise RuntimeError("fixture event publication failed")

    async def on_exit(code, stderr):
        exited.append((code, stderr))

    proc = Process()
    session = managed_session(proc, on_event=publication, on_exit=on_exit)
    session._reader = asyncio.create_task(session._read_loop())
    proc.send(tool_call())  # Deliberately missing the required rollout cursor.
    try:
        await asyncio.wait_for(asyncio.shield(session._reader), 1)
        assert len(exited) == 1, "process death must reach the durable terminal owner"
        assert exited[0][0] == -9
        assert "Tool usage/control failed" in exited[0][1]
        assert events == [], "process failure must not invent a second terminal event"
        assert proc.writes == []
        assert not session._pending and session._dynamic_tool_requests_idle.is_set()
    finally:
        await session.terminate(timeout=0.2)


@pytest.mark.asyncio
async def test_external_stop_drains_the_owned_callback(monkeypatch):
    from subagents import codex_tool_requests

    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def pending_usage(_self, _frame):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(codex_tool_requests.ToolRequests, "_usage", pending_usage)
    proc = Process()
    session = managed_session(proc)
    session._reader = asyncio.create_task(session._read_loop())
    proc.send(tool_call())
    await asyncio.wait_for(entered.wait(), 1)
    await session.terminate(timeout=0.2)
    assert cancelled.is_set()
    assert session._reader.done() and not session._tool_requests.tasks
    assert proc.writes == []


@pytest.mark.asyncio
async def test_rpc_identity_is_scoped_to_the_provider_turn(monkeypatch, tmp_path):
    from test_codex_durable_receipts import group, lines

    executed = []

    async def execute(*_args, **kwargs):
        executed.append(kwargs["provider_tool_call_id"])
        return "saved"

    monkeypatch.setattr("subagents.harness_tool_bridge.execute_tool", execute)
    proc = Process()
    session = managed_session(proc)
    declare_codex_tool_step(session, "call-turn", persist=False)
    declare_codex_tool_step(session, "call-next", persist=False)
    path = tmp_path / "rollout.jsonl"
    path.touch()
    session._tool_requests.rollout_path = str(path)
    session._reader = asyncio.create_task(session._read_loop())
    try:
        for turn in ("turn", "next"):
            await session._tool_requests.prepare_turn()
            session._track_active_turn(turn)
            with path.open("ab") as stream:
                stream.write(lines(*group(turn=turn, calls=(f"call-{turn}",))))
            await session._handle_message(tool_call(turn=turn))
            await asyncio.wait_for(session._dynamic_tool_requests_idle.wait(), 1)
        assert executed == ["call-turn", "call-next"]
        replies = [frame for frame in proc.writes if "result" in frame]
        assert len(replies) == 2 and all(frame["id"] == 900 for frame in replies)
    finally:
        await session.terminate(timeout=0.2)


@pytest.mark.asyncio
async def test_managed_callback_preserves_existing_unbindable_result_isolation(monkeypatch):
    from core._tool_result_delivery_support import ToolResultBindingError
    from subagents.codex_tool_requests import ToolRequests

    async def usage(_self, _frame):
        return {"last": {"inputTokens": 100, "outputTokens": 10},
                "total": {"inputTokens": 1000, "outputTokens": 100}}

    async def unbindable(*_args, **_kwargs):
        raise ToolResultBindingError("fixture result has no authoritative Step")

    monkeypatch.setattr(ToolRequests, "_usage", usage)
    monkeypatch.setattr("subagents.harness_tool_bridge.execute_tool", unbindable)
    proc = Process()
    session = managed_session(proc)
    session._reader = asyncio.create_task(session._read_loop())
    try:
        await session._handle_message(tool_call())
        await asyncio.wait_for(session._dynamic_tool_requests_idle.wait(), 1)
        assert proc.returncode is None and not session._closing
        assert not session._reader.done()
    finally:
        await session.terminate(timeout=0.2)
