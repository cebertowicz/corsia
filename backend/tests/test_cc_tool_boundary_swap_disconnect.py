"""A tool-boundary checkpoint swap survives retiring its own bridge caller.

Issue #1867: the swap runs inside the loopback request of the Claude Code
process it replaces. Killing that process closes the request, and the ASGI
server cancels the request task. Everything here is real (uvicorn, the bridge
route, the stdio MCP bridge, checkpoint install, swap, and a replacement
process) except the tool body and its reply accounting. A busy backend loop
is modeled by one blocking callback right after the kill: the retired bridge
child closes its request within milliseconds, so the loop then sees that
disconnect together with the retired process's exit, as a loaded backend does.
"""

from __future__ import annotations

import asyncio
import socket
import sys
import textwrap
import time
from dataclasses import replace
from pathlib import Path

import pytest
from core import fork_runtime

from cc_runtime_fixtures import cc_target, fake_claude, trust_live_tool_step_ids  # noqa: F401
from core.turn_core import TurnIdentity
from test_checkpoint_hold_install_resume import _seed, _start_checkpoint
from vendor_account_fixtures import point_default_home
from infra.default_port import DEFAULT_BACKEND_PORT

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login
DISCOVERED_MODELS = {"claude-code": (cc_target().catalog_id,)}  # the Default lists the model under test

_THREAD = "held-thread"
_SERVER = Path(__file__).resolve().parents[1] / "subagents" / "corsia_mcp_server.py"
_TOKEN = "swap-token"
_DEFINITIONS = ({"type": "function", "function": {
    "name": "test", "description": "test tool",
    "parameters": {"type": "object", "properties": {}},
}},)


def _retired_claude(tmp_path: Path, port: int) -> Path:
    """Write a stand-in claude that holds one real MCP bridge call open."""
    script = tmp_path / "retired_claude.py"
    script.write_text(textwrap.dedent(f"""
        import json, os, subprocess, sys
        env = dict(os.environ)
        env["CORSIA_CC_TOOL_URL"] = "http://127.0.0.1:{port}/api/cc/_tool-bridge/{_THREAD}"
        env["CORSIA_CC_TOOL_TOKEN"] = {_TOKEN!r}
        server = subprocess.Popen(
            [sys.executable, {str(_SERVER)!r}],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
        )
        def send(message):
            server.stdin.write((json.dumps(message) + "\\n").encode())
            server.stdin.flush()
        send({{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {{
            "protocolVersion": "2025-06-18", "capabilities": {{}},
            "clientInfo": {{"name": "retired-claude", "version": "0"}},
        }}}})
        server.stdout.readline()
        send({{"jsonrpc": "2.0", "method": "notifications/initialized"}})
        send({{"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {{
            "name": "test", "arguments": {{}},
            "_meta": {{"claudecode/toolUseId": "current-call"}},
        }}}})
        sys.stdin.buffer.read()
    """), encoding="utf-8")
    return script


async def _serve_bridge():
    import uvicorn
    from fastapi import FastAPI

    from api import cc_attach

    app = FastAPI()
    app.include_router(cc_attach.router)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(
        app, log_level="warning", lifespan="off", timeout_graceful_shutdown=1,
    ))
    task = asyncio.create_task(server._serve(sockets=[sock]))
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.01)
    return server, task, sock.getsockname()[1]


def _bridge_result(identity):
    from core._tool_result_delivery_support import ToolExecutionResult
    from subagents.harness_tool_bridge import ToolBridgeResult

    text = "EXACT_DURABLE_TOOL_RESULT"
    delivery = ToolExecutionResult(
        message_id="r10", assistant_step_id="a9", invocation_id="current-call",
        provider_tool_call_id="current-call", name="test", content=text,
        content_sha256="", content_byte_count=len(text), is_error=False,
    )
    return ToolBridgeResult(text, delivery, identity)


def _retiring_live(target, identity, tmp_path: Path, port: int, monkeypatch, on_kill):
    from core.fork_cc_context import CCForkContext
    from subagents.cc_runtime_state import _LIVE, _Live, activate_bound_turn
    from subagents.cc_stream import CCStreamSession

    session = CCStreamSession(binary="unused", session_id="old-provider",
        working_dir=str(tmp_path), claude_dir=str(tmp_path / "claude"), corsia_thread_id=_THREAD)
    script = _retired_claude(tmp_path, port)
    monkeypatch.setattr(session, "build_cmd", lambda: [sys.executable, str(script)])

    async def record_tool_reply(*_args):
        return None

    kill = session._request_terminate

    def busy_loop_after_kill():
        kill()
        asyncio.get_running_loop().call_soon(time.sleep, 0.3)
        on_kill()

    monkeypatch.setattr(session, "record_tool_reply", record_tool_reply)
    monkeypatch.setattr(session, "_request_terminate", busy_loop_after_kill)
    session.tool_context = CCForkContext(target, 32788)
    live = _Live(session=session, parent_thread_id=None, name="old",
        catalog_model_id=target.catalog_id, context_window=target.context_window,
        corsia_turn=identity, tool_registry=object(),
        tool_definitions=_DEFINITIONS, tool_token=_TOKEN,
        tool_step_ids={"current-call": "a9"})
    assert activate_bound_turn(live, identity)
    _LIVE.put(_THREAD, live)
    return live


def _turn_status() -> str:
    from infra.database import get_db

    db = get_db()
    try:
        row = db.execute("SELECT status FROM active_turns WHERE id='turn-current'").fetchone()
    finally:
        db.close()
    return row[0]


async def _settled_status() -> str:
    for _ in range(250):
        status = _turn_status()
        if status != "running":
            return status
        await asyncio.sleep(0.02)
    return status


@pytest.mark.asyncio
async def test_disconnect_leaves_durable_settlement_to_the_exact_live(monkeypatch):
    """A closed caller cannot cancel settlement; the exact live's owner still can."""
    from types import SimpleNamespace

    from starlette.requests import Request

    from api import cc_attach
    from subagents import harness_tool_bridge
    from subagents.cc_runtime_quiescence import cancel_admitted_tool_tasks
    from subagents.cc_runtime_state import _LIVE

    identity = SimpleNamespace(turn_id="turn-1")
    live = SimpleNamespace(
        tool_registry=object(), tool_token=_TOKEN,
        tool_step_ids={"current-call": "a9"}, corsia_turn=identity,
        admitted_tool_tasks=set(), turn_active=True, interrupt_requested=False,
    )
    settling, disconnected = asyncio.Event(), asyncio.Event()
    settlement: list[str] = []

    async def execute_tool(*_args, **_kwargs):
        return _bridge_result(identity)

    async def settle(*_args):
        settling.set()
        try:
            await disconnected.wait()
            await asyncio.sleep(0.05)
            settlement.append("survived disconnect")
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            settlement.append("cancelled")
            raise

    async def receive():
        await settling.wait()
        disconnected.set()
        return {"type": "http.disconnect"}

    async def send(_message):
        return None

    monkeypatch.setattr(cc_attach, "execute_tool", execute_tool)
    monkeypatch.setattr(harness_tool_bridge, "settle_cc_tool_result", settle)
    trust_live_tool_step_ids(monkeypatch)
    _LIVE.put("thread-1", live)
    path = "/api/cc/_tool-bridge/thread-1"
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"",
        "headers": [], "client": ("127.0.0.1", 9000),
        "server": ("127.0.0.1", DEFAULT_BACKEND_PORT),
    }
    response = await cc_attach.execute_cc_tool(
        Request(scope), "thread-1",
        {"name": "test", "arguments": {}, "provider_tool_call_id": "current-call"},
        _TOKEN,
    )
    call = asyncio.create_task(response(scope, receive, send))
    try:
        for _ in range(200):
            if settlement or call.done():
                break
            await asyncio.sleep(0.01)
        assert settlement == ["survived disconnect"]
        assert len(live.admitted_tool_tasks) == 1
        cancel_admitted_tool_tasks(live)
        await asyncio.wait_for(call, 5)
        assert settlement == ["survived disconnect", "cancelled"]
        assert not live.admitted_tool_tasks
    finally:
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        _LIVE.pop("thread-1", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [False, True])
async def test_cc_tool_boundary_swap_survives_its_retired_callers_disconnect(
    isolated_db, monkeypatch, tmp_path, fake_claude, stop,
):
    from api import cc_attach
    from core import checkpoint_runtime as runtime
    from history.adapters import MID_TURN_RESUME_ANCHOR
    from infra.database import get_db
    from registry.service import register_cc_session
    from subagents import cc_runtime
    from subagents.cc_runtime_state import _LIVE
    from subagents.cc_stream import CCStreamSession

    target = replace(cc_target(), context_window=32768)
    monkeypatch.setattr("llm.resolution.resolve_execution_target", lambda *_a, **_k: target)
    point_default_home("claude-code", tmp_path / "claude")  # the replacement runs in the account's home
    db = get_db()
    _seed(db, target.catalog_id)
    register_cc_session(_THREAD, "old-provider", checkpoint_generation_id="floor", db=db)
    db.commit()
    db.close()
    identity = TurnIdentity("turn-current", 0)
    server, serve_task, port = await _serve_bridge()
    stops: list[asyncio.Task] = []

    def stop_mid_swap():
        if stop and not stops:
            stops.append(asyncio.create_task(
                cc_runtime.stop(_THREAD, expected_turn_id=identity.turn_id),
            ))

    old = _retiring_live(target, identity, tmp_path, port, monkeypatch, stop_mid_swap)
    launches: list[CCStreamSession] = []

    async def execute_tool(*_args, **_kwargs):
        return _bridge_result(identity)

    def launch(session):
        launches.append(session)
        return [fake_claude]

    monkeypatch.setattr(cc_attach, "execute_tool", execute_tool)
    monkeypatch.setattr(CCStreamSession, "build_cmd", launch)
    monkeypatch.setattr(
        "subagents.cc_resume_transition._default_broadcast", lambda *_args: None,
    )
    swapped = asyncio.Event()
    outcome: list[str] = []
    resume = cc_runtime.resume_same_turn_after_checkpoint

    async def observed_resume(*args, **kwargs):
        try:
            result = await resume(*args, **kwargs)
            outcome.append("returned")
            return result
        except BaseException as error:
            outcome.append(type(error).__name__)
            raise
        finally:
            swapped.set()

    monkeypatch.setattr(cc_runtime, "resume_same_turn_after_checkpoint", observed_resume)
    release, _waiting, await_original = await _start_checkpoint(
        monkeypatch, target, tmp_path, identity,
    )
    release.set()
    assert (await asyncio.wait_for(await_original("new-checkpoint"), 5)).status == "installed"
    try:
        await old.session.start()
        await asyncio.wait_for(swapped.wait(), 20)
        assert old.session.returncode is not None
        assert len(launches) == 1
        replacement = launches[0]
        if stop:
            assert await asyncio.wait_for(stops[0], 10) is True
            # Stop's kill and the swap's pre-write checks race; either refusal
            # is correct as long as nothing reaches the replacement.
            assert outcome in (["CancelledError"], ["CCPreparedDestinationStale"])
            assert replacement.input_frame_log == []
            assert await _settled_status() == "cancelled"
            assert replacement.returncode is not None
            assert _LIVE.get(_THREAD) is None
        else:
            assert outcome == ["returned"]
            live = _LIVE.get(_THREAD)
            assert live.session is replacement and live.corsia_turn == identity
            assert MID_TURN_RESUME_ANCHOR in str(replacement.input_frame_log)
            assert await _settled_status() == "completed"
    finally:
        server.should_exit = True
        await asyncio.wait_for(serve_task, 10)
        await asyncio.gather(*stops, return_exceptions=True)
        await fork_runtime.shutdown_forks()
        await runtime.shutdown_checkpoint_wakes()
        for session in [old.session, *launches]:
            await session.terminate()
        _LIVE.reset()
