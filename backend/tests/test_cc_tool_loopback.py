from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi import HTTPException
from starlette.requests import Request

from cc_runtime_fixtures import (  # noqa: F401
    StubCCInvocation,
    _seed_parent,
    cc_target,
    child_db,
    prepared_cc_input,
    settle_test_input,
    trust_live_tool_step_ids,
)
from infra.default_port import DEFAULT_BACKEND_PORT


_BACKEND_DIR = Path(__file__).resolve().parents[1]
_BRIDGE_PATH = "/api/cc/_tool-bridge/thread-1"
_HEADER = {"X-CORSIA-Tool-Token": "live-secret"}


def _bridge_result(identity, *, text: str = "exact result", media=None):
    from core._tool_result_delivery_support import ToolExecutionResult
    from subagents.harness_tool_bridge import ToolBridgeResult

    delivery = ToolExecutionResult(
        message_id="msg-1",
        assistant_step_id="step-1",
        invocation_id="call-1",
        provider_tool_call_id="call-1",
        name="read_file",
        content=text,
        content_sha256="",
        content_byte_count=len(text),
        is_error=False,
    )
    return ToolBridgeResult(text, delivery, identity, media=media)


def _admit_active_turn(db, turn_id: str, *, text: str = "hello"):
    from core.turn_core import BOOT_EPOCH
    from infra.thread_driver import admit_input

    admission = admit_input(
        "main-1",
        role="user",
        content=text,
        intent="apply_now",
        message_id=f"{turn_id}:input",
        turn_id=turn_id,
        boot_epoch=BOOT_EPOCH,
        admitted_at="t",
        db=db,
    )
    assert admission.batch is not None
    settle_test_input(admission.batch)
    return admission.batch.identity


@pytest.fixture(autouse=True)
def _clear_cc_live():
    from subagents.cc_runtime_state import _LIVE, reset_tool_token_grace

    _LIVE.pop("thread-1", None)
    reset_tool_token_grace()
    yield
    _LIVE.pop("thread-1", None)
    reset_tool_token_grace()


async def _start_under_lock(resume, runtime, thread_id, **kwargs):
    lock = runtime._LIVE.lock(thread_id)
    async with lock:
        return await resume.start_cc_for_row(thread_id, held_lock=lock, **kwargs)


def test_stdio_bridge_imports_no_backend_tool_runtime():
    script = f"""
import json
import sys
sys.path.insert(0, {str(_BACKEND_DIR)!r})
import subagents.corsia_mcp_server
names = [
    "subagents.harness_tool_bridge", "infra.state", "core.agent", "tools.registry"
]
print(json.dumps([name for name in names if name in sys.modules]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )

    assert json.loads(result.stdout.strip().splitlines()[-1]) == []


def test_backend_launcher_exports_the_port_it_binds():
    source = (_BACKEND_DIR.parent / "scripts/corsia.d/runtime.sh").read_text(
        encoding="utf-8"
    )
    launch = source[source.index("-m uvicorn main:app") - 180:]
    assert re.search(
        r'BACKEND_PORT="\$BACKEND_PORT"[\s\\]*.*nohup.*-m uvicorn main:app',
        launch,
    )


def test_same_session_configs_are_isolated_and_cleaned_independently(monkeypatch):
    from subagents.cc_runtime_quiescence import revoke_tool_access
    from subagents.harness_tool_bridge import write_cc_mcp_config

    monkeypatch.setenv("BACKEND_PORT", str(DEFAULT_BACKEND_PORT))
    first_path = write_cc_mcp_config(
        thread_id="shared-thread",
        tool_token="first-secret",
    )
    monkeypatch.setenv("BACKEND_PORT", "3001")
    second_path = write_cc_mcp_config(
        thread_id="shared-thread",
        tool_token="second-secret",
    )
    first = SimpleNamespace(
        session=SimpleNamespace(mcp_config=first_path),
        tool_config=first_path,
        tool_registry=object(),
        tool_token="first-secret",
    )
    second = SimpleNamespace(
        session=SimpleNamespace(mcp_config=second_path),
        tool_config=second_path,
        tool_registry=object(),
        tool_token="second-secret",
    )

    try:
        assert first_path != second_path
        assert json.loads(Path(first_path).read_text(encoding="utf-8"))[
            "mcpServers"
        ]["corsia"]["env"] == {
            "CORSIA_CC_TOOL_TOKEN": "first-secret",
            "CORSIA_CC_TOOL_URL": (
                f"http://127.0.0.1:{DEFAULT_BACKEND_PORT}/api/cc/_tool-bridge/shared-thread"
            ),
        }
        assert json.loads(Path(second_path).read_text(encoding="utf-8"))[
            "mcpServers"
        ]["corsia"]["env"] == {
            "CORSIA_CC_TOOL_TOKEN": "second-secret",
            "CORSIA_CC_TOOL_URL": (
                "http://127.0.0.1:3001/api/cc/_tool-bridge/shared-thread"
            ),
        }

        revoke_tool_access(first)
        assert not Path(first_path).exists()
        assert Path(second_path).exists()

        revoke_tool_access(second)
        assert not Path(second_path).exists()
    finally:
        Path(first_path).unlink(missing_ok=True)
        Path(second_path).unlink(missing_ok=True)


async def _client(route_module, *, host: str = "127.0.0.1"):
    app = FastAPI()
    app.include_router(route_module.router)
    transport = httpx.ASGITransport(app=app, client=(host, 9000))
    return app, httpx.AsyncClient(transport=transport, base_url="http://corsia")


@pytest.mark.asyncio
async def test_private_route_authenticates_exact_live_and_derives_authority(
    monkeypatch,
):
    from api import cc_attach as route_module
    from subagents.cc_runtime_state import _LIVE

    trust_live_tool_step_ids(monkeypatch)
    registry = object()
    definitions = ({"type": "function", "function": {"name": "read_file"}},)
    identity = SimpleNamespace(turn_id="turn-1")
    _LIVE.put("thread-1", SimpleNamespace(
        tool_registry=registry,
        tool_definitions=definitions,
        tool_token="live-secret",
        tool_step_ids={"call-1": "step-1"},
        corsia_turn=identity,
        admitted_tool_tasks=set(),
        session=None,
        turn_active=True,
        interrupt_requested=False,
    ))
    execute = AsyncMock(return_value=_bridge_result(identity))
    monkeypatch.setattr(route_module, "execute_tool", execute)
    from subagents import harness_tool_bridge

    monkeypatch.setattr(
        harness_tool_bridge,
        "settle_cc_tool_result",
        AsyncMock(return_value=False),
    )

    app, client = await _client(route_module)
    async with client:
        assert (await client.get(_BRIDGE_PATH)).status_code == 401
        assert (
            await client.get(
                _BRIDGE_PATH,
                headers={"X-CORSIA-Tool-Token": "wrong"},
            )
        ).status_code == 401
        listed = await client.get(_BRIDGE_PATH, headers=_HEADER)
        called = await client.post(
            _BRIDGE_PATH,
            headers=_HEADER,
            json={
                "name": "read_file",
                "arguments": {"path": "README.md"},
                "provider_tool_call_id": "call-1",
            },
        )

    assert listed.json() == {"tools": list(definitions)}
    assert called.text == "exact result"
    call_kwargs = execute.await_args.kwargs
    assert call_kwargs["thread_id"] == "thread-1"
    assert call_kwargs["name"] == "read_file"
    assert call_kwargs["arguments"] == {"path": "README.md"}
    assert _BRIDGE_PATH not in app.openapi()["paths"]

    _LIVE.pop("thread-1")
    _, stale_client = await _client(route_module)
    async with stale_client:
        assert (await stale_client.get(_BRIDGE_PATH, headers=_HEADER)).status_code == 401

    _LIVE.put("thread-1", SimpleNamespace(
        tool_registry=registry,
        tool_token="live-secret",
        tool_step_ids={},
    ))
    _, remote_client = await _client(route_module, host="192.0.2.1")
    async with remote_client:
        assert (await remote_client.get(_BRIDGE_PATH, headers=_HEADER)).status_code == 403

    for forwarding in ({"Tailscale-User-Login": "a@example.com"}, {"X-Forwarded-For": "100.64.0.9"}):
        _, forwarded_client = await _client(route_module)
        async with forwarded_client:
            refused = await forwarded_client.get(_BRIDGE_PATH, headers={**_HEADER, **forwarding})
        assert refused.status_code == 403


@pytest.mark.asyncio
async def test_private_route_signs_media_with_callers_authenticated_secret(monkeypatch):
    """Only the authenticated bridge route can produce a usable media envelope."""
    from mcp.types import ImageContent, TextContent

    from api import cc_attach as route_module
    from core._tool_result_delivery_support import with_invocation_reference
    from core.media_delivery import ToolMediaContent
    from core.media_projection import ValidatedImage
    from subagents import corsia_mcp_server
    from subagents.cc_runtime_state import _LIVE, record_tool_token_rotation

    trust_live_tool_step_ids(monkeypatch)
    identity = SimpleNamespace(turn_id="turn-1")
    projected = with_invocation_reference("[Image: shot.png]", "call-1")
    media = ToolMediaContent(
        text="[Image: shot.png]",
        image=ValidatedImage(
            attachment_id="image-1",
            mime_type="image/png",
            data=b"image-bytes",
            sha256="0" * 64,
            byte_size=len(b"image-bytes"),
        ),
    )
    record_tool_token_rotation("thread-1", "live-secret")
    _LIVE.put("thread-1", SimpleNamespace(
        tool_registry=object(),
        tool_definitions=(),
        tool_token="successor-secret",
        tool_step_ids={"call-1": "step-1"},
        corsia_turn=identity,
        admitted_tool_tasks=set(),
        session=None,
        turn_active=True,
        interrupt_requested=False,
    ))
    monkeypatch.setattr(
        route_module,
        "execute_tool",
        AsyncMock(return_value=_bridge_result(identity, text=projected, media=media)),
    )
    from subagents import harness_tool_bridge

    monkeypatch.setattr(
        harness_tool_bridge,
        "settle_cc_tool_result",
        AsyncMock(return_value=False),
    )

    _, client = await _client(route_module)
    async with client:
        response = await client.post(
            _BRIDGE_PATH,
            headers={"X-CORSIA-Tool-Token": "live-secret"},
            json={
                "name": "read_file",
                "arguments": {"path": "shot.png"},
                "provider_tool_call_id": "call-1",
            },
        )

    blocks = corsia_mcp_server.tool_result_content(response.text, secret="live-secret")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert isinstance(blocks[0], TextContent)
    assert blocks[0].text == projected
    assert isinstance(blocks[1], ImageContent)
    assert blocks[1].mimeType == "image/png"
    assert blocks[1].data == "aW1hZ2UtYnl0ZXM="


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["ordinary output", "Error: exact tool failure"])
async def test_stdio_http_client_preserves_tool_result_text(result):
    from subagents.corsia_mcp_server import _call_tool

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-CORSIA-Tool-Token"] == "live-secret"
        body = json.loads(request.content)
        assert body["name"] == "read_file"
        assert body["arguments"] == {"path": "README.md"}
        assert "invocation_id" in body
        return httpx.Response(200, text=result)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers=_HEADER,
    ) as client:
        actual = await _call_tool(
            client,
            f"http://127.0.0.1:{DEFAULT_BACKEND_PORT}/api/cc/_tool-bridge/thread-1",
            "read_file",
            {"path": "README.md"},
        )

    assert actual == result


@pytest.mark.asyncio
async def test_stdio_http_client_ignores_environment_proxies():
    from subagents import corsia_mcp_server

    client = corsia_mcp_server._http_client("live-secret", "cp-1")
    try:
        assert client._trust_env is False
        assert client.headers["X-CORSIA-Checkpoint-Id"] == "cp-1"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_loopback_waits_for_the_exact_provider_assistant_step(monkeypatch):
    """An early MCP call resumes only after its durable provider step exists."""
    from api import cc_attach
    from subagents import cc_event_steps
    from subagents.cc_runtime_state import _Live

    live = _Live(session=None, parent_thread_id=None, name="cc")
    waiting = asyncio.create_task(
        cc_attach._assistant_step_for_call(live, "call-1")
    )
    await asyncio.sleep(0)
    monkeypatch.setattr(
        cc_event_steps, "frame", lambda _ctx, _event, payload: payload
    )
    context = SimpleNamespace(
        live=live,
        child_id="thread-1",
        bcast=lambda _thread_id, _payload: None,
    )

    cc_event_steps._broadcast_tool_starts(
        context,
        {},
        [{"id": "other-call", "name": "Read", "arguments": {}, "hint": ""}],
        "other-step",
    )
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(waiting), timeout=0.05)

    cc_event_steps._broadcast_tool_starts(
        context,
        {},
        [{"id": "call-1", "name": "Read", "arguments": {}, "hint": ""}],
        "step-1",
    )

    assert await asyncio.wait_for(waiting, timeout=1) == "step-1"


@pytest.mark.asyncio
async def test_loopback_disconnect_cancels_backend_owned_execution(monkeypatch):
    from api import cc_attach as route_module
    from subagents.cc_runtime_state import _LIVE

    trust_live_tool_step_ids(monkeypatch)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_execute(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    _LIVE.put("thread-1", SimpleNamespace(
        tool_registry=object(),
        tool_token="live-secret",
        tool_step_ids={"call-1": "step-1"},
        corsia_turn=SimpleNamespace(turn_id="turn-1"),
        admitted_tool_tasks=set(),
        turn_active=True,
        interrupt_requested=False,
    ))
    monkeypatch.setattr(route_module, "execute_tool", blocked_execute)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": _BRIDGE_PATH,
        "raw_path": _BRIDGE_PATH.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 9000),
        "server": ("127.0.0.1", DEFAULT_BACKEND_PORT),
    }
    request = Request(scope)
    response = await route_module.execute_cc_tool(
        request,
        "thread-1",
        {
            "name": "slow",
            "arguments": {},
            "provider_tool_call_id": "call-1",
        },
        "live-secret",
    )

    async def receive():
        await started.wait()
        return {"type": "http.disconnect"}

    async def send(_message):
        return None

    await response(scope, receive, send)

    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_finalize_refuses_to_pop_a_live_with_an_admitted_tool_task():
    from subagents.cc_runtime_lifecycle import finalize
    from subagents.cc_runtime_state import _LIVE, _Live

    release = asyncio.Event()
    tool_task = asyncio.create_task(release.wait())
    live = _Live(
        session=None,
        parent_thread_id=None,
        name="cc",
    )
    live.admitted_tool_tasks.add(tool_task)
    _LIVE.put("thread-1", live)

    try:
        assert finalize(
            "thread-1",
            lambda *_args: None,
            emit_closed=False,
        ) is None
        assert _LIVE.get("thread-1") is live
    finally:
        live.admitted_tool_tasks.discard(tool_task)
        release.set()
        await asyncio.wait_for(tool_task, timeout=1)
        _LIVE.pop("thread-1", None)


@pytest.mark.asyncio
async def test_execution_rechecks_live_handle_when_stream_starts(
    monkeypatch,
):
    from api import cc_attach as route_module
    from subagents.cc_runtime_state import _LIVE

    registry = object()
    original = SimpleNamespace(
        tool_registry=registry,
        tool_token="live-secret",
        admitted_tool_tasks=set(),
        turn_active=True,
        interrupt_requested=False,
    )
    _LIVE.put("thread-1", original)
    execute = AsyncMock(return_value="must not execute")
    monkeypatch.setattr(route_module, "execute_tool", execute)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": _BRIDGE_PATH,
        "raw_path": _BRIDGE_PATH.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 9000),
        "server": ("127.0.0.1", DEFAULT_BACKEND_PORT),
    }
    replacement_response = await route_module.execute_cc_tool(
        Request(scope),
        "thread-1",
        {"name": "read_file", "arguments": {}},
        "live-secret",
    )
    # A real replacement revokes (recording the rotation) before installing
    # the successor. Even inside the grace window, execution admitted under
    # the retired handle must not start under the successor's authority.
    from subagents.cc_runtime_state import record_tool_token_rotation

    record_tool_token_rotation("thread-1", "live-secret")
    _LIVE.put("thread-1", SimpleNamespace(
        tool_registry=object(),
        tool_token="replacement-secret",
    ))
    sent: list[dict] = []

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        sent.append(message)

    with pytest.raises(HTTPException, match="Claude Code tool token rotated"):
        await replacement_response(scope, receive, send)
    assert sent == []
    execute.assert_not_awaited()


def test_finalize_clears_ephemeral_tool_access(tmp_path):
    from subagents.cc_runtime_lifecycle import finalize
    from subagents.cc_runtime_state import _LIVE

    seed = tmp_path / "seed.md"
    config = tmp_path / "mcp.json"
    seed.write_text("seed", encoding="utf-8")
    config.write_text("secret", encoding="utf-8")
    live = SimpleNamespace(
        session=SimpleNamespace(mcp_config=str(config)),
        seed_file=str(seed),
        tool_config=str(config),
        parent_thread_id=None,
        tool_registry=object(),
        tool_token="live-secret",
        admitted_tool_tasks=set(),
    )
    _LIVE.put("thread-1", live)

    assert finalize("thread-1", lambda *_args: None, emit_closed=False) is live

    assert not seed.exists()
    assert not config.exists()
    assert live.tool_registry is None
    assert live.tool_token == ""
    assert live.tool_config == ""


@pytest.mark.asyncio
async def test_failed_cc_start_cleans_early_live_and_config(
    main_db, tmp_path, monkeypatch,
):
    from subagents import cc_runtime as runtime
    from subagents import cc_destination, cc_startup_process
    from subagents import cc_runtime_helpers as runtime_helpers
    from subagents import harness_tool_bridge

    registry = object()
    config = tmp_path / "mcp.json"
    seen: dict[str, str] = {}
    terminated = False

    def write_config(**kwargs):
        seen["token"] = kwargs["tool_token"]
        config.write_text("secret", encoding="utf-8")
        return str(config)

    class FailingSession(StubCCInvocation):
        def __init__(self, **kwargs):
            self.mcp_config = kwargs["mcp_config"]

        async def start(self):
            live = runtime._LIVE.get("main-1")
            assert live is not None
            assert live.tool_registry is registry
            assert live.tool_token == seen["token"]
            raise RuntimeError("start broke")

        async def terminate(self):
            nonlocal terminated
            terminated = True

    monkeypatch.setattr(harness_tool_bridge, "build_tool_registry", lambda _cwd: registry)
    monkeypatch.setattr(
        harness_tool_bridge,
        "dynamic_tool_definitions",
        lambda _registry: (),
    )
    monkeypatch.setattr(harness_tool_bridge, "write_cc_mcp_config", write_config)
    monkeypatch.setattr(cc_startup_process, "CCStreamSession", FailingSession)
    monkeypatch.setattr(runtime_helpers, "_validate_working_dir", lambda *_args: None)
    monkeypatch.setattr(cc_destination, "_main_working_dir", lambda *_args: str(tmp_path))

    target = cc_target()
    prepared = await prepared_cc_input("main-1", "hello", target)
    with pytest.raises(RuntimeError, match="start broke"):
        await runtime.feed_main(
            "main-1", "hello", target=target, binary="claude",
            turn_id="turn-main-1", claude_dir=str(tmp_path / "claude-home"),
            prepared_context=prepared,
        )

    assert runtime._LIVE.get("main-1") is None
    assert not config.exists()
    assert terminated is True


def _registered_transcript(monkeypatch, tmp_path) -> None:
    """Put registered session cc-1 where its launch resumes it, so Claude is what rejects it."""
    root = tmp_path / ".claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
    transcript = root / "projects" / "-registered" / "cc-1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")


@pytest.mark.asyncio
async def test_resume_rejection_immediately_revokes_tool_authority(
    main_db, tmp_path, monkeypatch,
):
    from subagents import cc_runtime as runtime
    from subagents import cc_runtime_errors
    from subagents import cc_runtime_resume as resume
    from subagents import cc_destination, cc_startup_context, cc_startup_process
    from subagents import cc_runtime_helpers as runtime_helpers
    from subagents import cc_sessions

    captured: dict[str, object] = {}
    _registered_transcript(monkeypatch, tmp_path)

    class RejectableSession(StubCCInvocation):
        returncode = None

        def __init__(self, **kwargs):
            self.mcp_config = kwargs["mcp_config"]
            self.on_exit = kwargs["on_exit"]

        async def start(self):
            return None

    monkeypatch.setattr(cc_startup_process, "CCStreamSession", RejectableSession)
    monkeypatch.setattr(cc_destination, "_artifact_cc_session_id", lambda *_args: "cc-1")
    monkeypatch.setattr(cc_startup_context, "_artifact_cc_session_id", lambda *_args: "cc-1")
    monkeypatch.setattr(runtime_helpers, "_validate_working_dir", lambda *_args: None)
    monkeypatch.setattr(cc_destination, "_main_working_dir", lambda *_args: str(tmp_path))
    monkeypatch.setattr(
        cc_sessions,
        "find_cc_session",
        lambda *_args, **_kwargs: {
            "path": str(tmp_path / "cc-1.jsonl"),
            "cwd": str(tmp_path),
        },
    )
    monkeypatch.setattr(cc_runtime_errors, "is_resume_not_found", lambda _stderr: True)

    from infra.queries import insert_message
    from registry.service import register_cc_session

    insert_message(
        "main-1", "user", "prior turn",
        {"turn_unbound_reason": "legacy_import"},
        msg_id="cc-resume-rejection-prior", db=main_db,
    )
    register_cc_session("main-1", "cc-1", db=main_db)
    main_db.commit()
    target = cc_target()
    prepared = await prepared_cc_input("main-1", "", target)
    live = await _start_under_lock(
        resume, runtime, "main-1",
        target=target,
        binary="claude",
        is_subagent=False,
        current_user_text="",
        prepared_context=prepared,
        surface_exit_error_fn=AsyncMock(),
        finalize_fn=runtime._finalize,
    )
    assert live is not None
    captured["config"] = live.tool_config
    assert Path(str(captured["config"])).exists()

    await live.session.on_exit(1, "resume rejected")

    assert runtime._LIVE.get("main-1") is live
    assert live.tool_registry is None
    assert live.tool_token == ""
    assert live.tool_config == ""
    assert live.pending_process_exit is None
    assert live.process_exit_resolved is False
    assert not Path(str(captured["config"])).exists()
    runtime._finalize("main-1", lambda *_args: None, emit_closed=False)


@pytest.mark.asyncio
async def test_post_acceptance_resume_rejection_finishes_the_admitted_turn(
    main_db, tmp_path, monkeypatch, caplog,
):
    """A delayed missing-session exit cannot leave an accepted Turn running."""
    from subagents import cc_runtime as runtime
    from subagents import cc_runtime_errors
    from subagents import cc_runtime_resume as resume
    from subagents import cc_destination, cc_startup_context, cc_startup_process
    from subagents import cc_runtime_helpers as runtime_helpers
    from subagents import cc_sessions

    _registered_transcript(monkeypatch, tmp_path)

    class RejectableSession(StubCCInvocation):
        returncode = 1

        def __init__(self, **kwargs):
            self.mcp_config = kwargs["mcp_config"]
            self.on_exit = kwargs["on_exit"]
            self.resume_session_id = kwargs["resume_session_id"]
            self.reconstructed_transcript_path = kwargs["reconstructed_transcript_path"]

        async def start(self):
            return None

        async def terminate(self):
            return None

        async def close(self):
            return self.returncode

    artifact_session_id = cc_startup_context._artifact_cc_session_id
    destination_session_id = cc_destination._artifact_cc_session_id
    monkeypatch.setattr(cc_startup_process, "CCStreamSession", RejectableSession)
    monkeypatch.setattr(cc_destination, "_artifact_cc_session_id", lambda *_args: "cc-1")
    monkeypatch.setattr(cc_startup_context, "_artifact_cc_session_id", lambda *_args: "cc-1")
    monkeypatch.setattr(runtime_helpers, "_validate_working_dir", lambda *_args: None)
    monkeypatch.setattr(cc_destination, "_main_working_dir", lambda *_args: str(tmp_path))
    monkeypatch.setattr(
        cc_sessions,
        "find_cc_session",
        lambda *_args, **_kwargs: {
            "path": str(tmp_path / "cc-1.jsonl"),
            "cwd": str(tmp_path),
        },
    )
    monkeypatch.setattr(cc_runtime_errors, "is_resume_not_found", lambda _stderr: True)

    from infra.queries import insert_message
    from registry.listing import find_for_thread
    from registry.service import register_cc_session

    insert_message(
        "main-1", "user", "prior turn",
        {"turn_unbound_reason": "legacy_import"},
        msg_id="cc-delayed-resume-rejection-prior", db=main_db,
    )
    register_cc_session("main-1", "cc-1", db=main_db)
    main_db.commit()
    target = cc_target()
    prepared = await prepared_cc_input("main-1", "", target)
    casts: list[tuple[str, dict]] = []
    live = await _start_under_lock(
        resume, runtime, "main-1",
        target=target,
        binary="claude",
        is_subagent=False,
        current_user_text="",
        prepared_context=prepared,
        broadcast=lambda thread_id, payload: casts.append((thread_id, payload)),
        surface_exit_error_fn=runtime._finish_process_exit,
        finalize_fn=runtime._finalize,
    )
    assert live is not None
    identity = _admit_active_turn(main_db, "cc-delayed-resume-rejection")
    live.corsia_turn = identity
    live.turn_active = True
    main_db.commit()

    from infra import db_async

    real_run_in_db = db_async.run_in_db
    run_in_db_calls: list[tuple[object, tuple, dict]] = []

    async def _tracking_run_in_db(fn, *args, **kwargs):
        run_in_db_calls.append((fn, args, kwargs))
        return await real_run_in_db(fn, *args, **kwargs)

    monkeypatch.setattr(db_async, "run_in_db", _tracking_run_in_db)

    await live.session.on_exit(1, "resume rejected")

    turn = main_db.execute(
        "SELECT status, error FROM active_turns WHERE id=?", (identity.turn_id,),
    ).fetchone()
    assert turn["status"] == "error"
    assert turn["error"] == "resume rejected"
    assert runtime._LIVE.get("main-1") is None
    assert find_for_thread("main-1", "claude-code", "session", db=main_db) is None
    assert "main-1 resume rejected after admission" in caplog.text
    # The retirement runs off the event loop through the DB thread pool, the
    # same pattern claim_terminal uses, so a busy writer cannot stall the loop.
    assert len(run_in_db_calls) == 1
    retire_fn, _retire_args, _retire_kwargs = run_in_db_calls[0]
    assert retire_fn.__name__ == "retire_rejected_provider_session"
    assert any(
        payload.get("message", {}).get("metadata", {}).get("type") == "cc_error"
        for _, payload in casts
    )
    assert any(
        payload.get("type") == "turn_complete"
        and payload.get("turn_id") == identity.turn_id
        for _, payload in casts
    )

    monkeypatch.setattr(cc_destination, "_artifact_cc_session_id", destination_session_id)
    monkeypatch.setattr(cc_startup_context, "_artifact_cc_session_id", artifact_session_id)
    retry = await prepared_cc_input("main-1", "retry", target)
    rebuilt = await _start_under_lock(
        resume, runtime, "main-1",
        target=target,
        binary="claude",
        is_subagent=False,
        current_user_text="retry",
        prepared_context=retry,
        surface_exit_error_fn=runtime._finish_process_exit,
        finalize_fn=runtime._finalize,
    )
    assert rebuilt is not None
    assert rebuilt.session.resume_session_id != "cc-1"
    assert rebuilt.session.reconstructed_transcript_path is not None
    runtime._finalize("main-1", lambda *_args: None, emit_closed=False)


@pytest.mark.asyncio
async def test_post_acceptance_resume_rejection_settles_even_when_retirement_fails(
    main_db, tmp_path, monkeypatch, caplog,
):
    """A failed session retirement cannot skip the terminal path.

    Retirement runs before the terminal path so Retry does not resume the
    dead session, but if it raises (a locked writer, a transaction-ownership
    error) the admitted Turn must still settle: otherwise it stays running
    with no live provider handle, the exact stall #2412 removes.
    """
    from subagents import cc_resume_transition
    from subagents import cc_runtime as runtime
    from subagents import cc_runtime_errors
    from subagents import cc_runtime_resume as resume
    from subagents import cc_destination, cc_startup_context, cc_startup_process
    from subagents import cc_runtime_helpers as runtime_helpers
    from subagents import cc_sessions

    _registered_transcript(monkeypatch, tmp_path)

    class RejectableSession(StubCCInvocation):
        returncode = 1

        def __init__(self, **kwargs):
            self.mcp_config = kwargs["mcp_config"]
            self.on_exit = kwargs["on_exit"]

        async def start(self):
            return None

        async def terminate(self):
            return None

        async def close(self):
            return self.returncode

    monkeypatch.setattr(cc_startup_process, "CCStreamSession", RejectableSession)
    monkeypatch.setattr(cc_destination, "_artifact_cc_session_id", lambda *_args: "cc-1")
    monkeypatch.setattr(cc_startup_context, "_artifact_cc_session_id", lambda *_args: "cc-1")
    monkeypatch.setattr(runtime_helpers, "_validate_working_dir", lambda *_args: None)
    monkeypatch.setattr(cc_destination, "_main_working_dir", lambda *_args: str(tmp_path))
    monkeypatch.setattr(
        cc_sessions,
        "find_cc_session",
        lambda *_args, **_kwargs: {
            "path": str(tmp_path / "cc-1.jsonl"),
            "cwd": str(tmp_path),
        },
    )
    monkeypatch.setattr(cc_runtime_errors, "is_resume_not_found", lambda _stderr: True)

    def _raise_locked(*_args, **_kwargs):
        raise RuntimeError("forced retirement failure: locked writer")

    monkeypatch.setattr(
        cc_resume_transition, "retire_rejected_provider_session", _raise_locked,
    )

    from infra.queries import insert_message
    from registry.listing import find_for_thread
    from registry.service import register_cc_session

    insert_message(
        "main-1", "user", "prior turn",
        {"turn_unbound_reason": "legacy_import"},
        msg_id="cc-retirement-failure-prior", db=main_db,
    )
    register_cc_session("main-1", "cc-1", db=main_db)
    main_db.commit()
    target = cc_target()
    prepared = await prepared_cc_input("main-1", "", target)
    casts: list[tuple[str, dict]] = []
    live = await _start_under_lock(
        resume, runtime, "main-1",
        target=target,
        binary="claude",
        is_subagent=False,
        current_user_text="",
        prepared_context=prepared,
        broadcast=lambda thread_id, payload: casts.append((thread_id, payload)),
        surface_exit_error_fn=runtime._finish_process_exit,
        finalize_fn=runtime._finalize,
    )
    assert live is not None
    identity = _admit_active_turn(main_db, "cc-retirement-failure")
    live.corsia_turn = identity
    live.turn_active = True
    main_db.commit()

    await live.session.on_exit(1, "resume rejected")

    turn = main_db.execute(
        "SELECT status, error FROM active_turns WHERE id=?", (identity.turn_id,),
    ).fetchone()
    assert turn["status"] == "error"
    assert turn["error"] == "resume rejected"
    # The Turn still settles and the live handle is still retired even though
    # retirement of the dead provider session failed.
    assert runtime._LIVE.get("main-1") is None
    # The registration itself was never retired: it stays as evidence for the
    # loud failure log below, not silently dropped.
    registration = find_for_thread("main-1", "claude-code", "session", db=main_db)
    assert registration is not None
    assert registration["external_id"] == "cc-1"
    assert "main-1 failed to retire the missing resume session" in caplog.text
    assert "main-1 resume rejected after admission" in caplog.text
    assert any(
        payload.get("message", {}).get("metadata", {}).get("type") == "cc_error"
        for _, payload in casts
    )
    assert any(
        payload.get("type") == "turn_complete"
        and payload.get("turn_id") == identity.turn_id
        for _, payload in casts
    )
    runtime._finalize("main-1", lambda *_args: None, emit_closed=False)


@pytest.mark.asyncio
async def test_rejected_resume_does_not_replace_an_unsettled_process(monkeypatch):
    from core.turn_core import TurnIdentity
    from subagents import cc_feed_transition
    from subagents import cc_resume_transition
    from subagents import cc_runtime as runtime

    target = cc_target()
    live = SimpleNamespace(
        synthetic_resume=False,
        resume_rejected=True,
        corsia_turn=TurnIdentity("turn-1", 1),
        checkpoint_wait_scope=object(),
        catalog_model_id=target.catalog_id,
        session=SimpleNamespace(resume_session_id="missing-cc"),
        tool_step_ids={"step-1"},
        tool_token="",
    )
    runtime._LIVE.reset()
    runtime._LIVE.put("thread-1", live)
    monkeypatch.setattr(
        cc_feed_transition, "retire_rejected_provider_session", lambda *_args: True,
    )
    resume = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(cc_resume_transition, "resume_child", resume)
    transition = cc_feed_transition.CCFeedTransition(
        thread_id="thread-1", text="hello", target=target, effort=None,
        claude_dir=None, binary=None, broadcast=None, is_subagent=False,
        bcast=lambda *_args: None, db=None,
        prepared_context=SimpleNamespace(),
        refresh_callback=AsyncMock(return_value=SimpleNamespace()),
        held_lock=runtime._LIVE.lock("thread-1"),
    )
    monkeypatch.setattr(cc_feed_transition, "require_cc_provider_fit", lambda *_: None)
    monkeypatch.setattr(
        "llm.resolution.resolve_execution_target", lambda _catalog_id: target,
    )

    try:
        async with transition.held_lock:
            with pytest.raises(RuntimeError, match="not settled"):
                await transition.rebuild_rejected_resume(live)
        assert runtime._LIVE.get("thread-1") is live
        resume.assert_not_awaited()
    finally:
        runtime._LIVE.reset()


@pytest.mark.asyncio
async def test_exit_callback_failure_still_finalizes_tool_authority(
    main_db, tmp_path, monkeypatch,
):
    from subagents import cc_runtime as runtime
    from subagents import cc_runtime_resume as resume
    from subagents import cc_destination, cc_startup_process
    from subagents import cc_runtime_helpers as runtime_helpers

    class ExitableSession(StubCCInvocation):
        returncode = None

        def __init__(self, **kwargs):
            self.mcp_config = kwargs["mcp_config"]
            self.on_exit = kwargs["on_exit"]

        async def start(self):
            return None

        async def terminate(self):
            return None

    monkeypatch.setattr(cc_startup_process, "CCStreamSession", ExitableSession)
    monkeypatch.setattr(runtime_helpers, "_validate_working_dir", lambda *_args: None)
    monkeypatch.setattr(cc_destination, "_main_working_dir", lambda *_args: str(tmp_path))
    async def finish_then_surface_failure(*args, **kwargs):
        await runtime._finish_process_exit(*args, **kwargs)
        raise RuntimeError("surface broke")

    target = cc_target()
    prepared = await prepared_cc_input("main-1", "", target)
    live = await _start_under_lock(
        resume, runtime, "main-1",
        target=target,
        binary="claude",
        claude_dir=str(tmp_path / "claude-home"),
        is_subagent=False,
        current_user_text="",
        prepared_context=prepared,
        surface_exit_error_fn=finish_then_surface_failure,
        finalize_fn=runtime._finalize,
    )
    assert live is not None
    live.corsia_turn = _admit_active_turn(
        main_db, "cc-post-terminal-surface",
    )
    live.turn_active = True
    main_db.commit()
    config = Path(live.tool_config)

    try:
        with pytest.raises(RuntimeError, match="surface broke"):
            await live.session.on_exit(1, "unexpected exit")
        assert runtime._LIVE.get("main-1") is None
        assert not config.exists()
        assert live.tool_token == ""
    finally:
        runtime._LIVE.pop("main-1", None)


@pytest.mark.asyncio
async def test_process_exit_retains_guard_when_admitted_tool_does_not_quiesce(
    main_db,
    tmp_path,
    monkeypatch,
):
    from subagents import cc_runtime as runtime
    from subagents import cc_runtime_lifecycle as lifecycle
    from subagents import cc_runtime_resume as resume
    from subagents import cc_destination, cc_process_exit, cc_startup_process
    from subagents import cc_runtime_helpers as runtime_helpers

    class ExitableSession(StubCCInvocation):
        returncode = 1

        def __init__(self, **kwargs):
            self.mcp_config = kwargs["mcp_config"]
            self.on_exit = kwargs["on_exit"]

        async def start(self):
            return None

        async def terminate(self):
            return None

        async def close(self):
            return self.returncode

    monkeypatch.setattr(cc_startup_process, "CCStreamSession", ExitableSession)
    monkeypatch.setattr(runtime_helpers, "_validate_working_dir", lambda *_args: None)
    monkeypatch.setattr(cc_destination, "_main_working_dir", lambda *_args: str(tmp_path))
    monkeypatch.setattr(cc_process_exit, "_CLOSE_LIVE_TIMEOUT_S", 0.01)
    surface = AsyncMock()
    target = cc_target()
    prepared = await prepared_cc_input("main-1", "", target)
    live = await _start_under_lock(
        resume, runtime, "main-1",
        target=target,
        binary="claude",
        claude_dir=str(tmp_path / "claude-home"),
        is_subagent=False,
        current_user_text="",
        prepared_context=prepared,
        surface_exit_error_fn=surface,
        finalize_fn=runtime._finalize,
    )
    assert live is not None
    identity = _admit_active_turn(main_db, "cc-exit-quiescence-turn")
    live.corsia_turn = identity
    live.turn_active = True
    main_db.commit()
    release = asyncio.Event()
    cancelled = asyncio.Event()

    async def resistant_tool():
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    tool_task = asyncio.create_task(resistant_tool())
    live.admitted_tool_tasks.add(tool_task)
    await asyncio.sleep(0)

    try:
        with pytest.raises(RuntimeError, match="did not quiesce"):
            await live.session.on_exit(1, "unexpected exit")
        assert cancelled.is_set()
        assert runtime._LIVE.get("main-1") is live
        assert live.provider_context_untrusted is True
        assert live.pending_process_exit is not None
        assert live.pending_process_exit.identity == identity
        assert live.pending_process_exit.code == 1
        assert live.pending_process_exit.stderr == "unexpected exit"
        surface.assert_not_awaited()
    finally:
        release.set()
        await asyncio.wait_for(tool_task, timeout=1)
        live.admitted_tool_tasks.discard(tool_task)
    assert await lifecycle.retire_live(
        "main-1", lambda *_args: None,
    ) is True
    turn = main_db.execute(
        "SELECT status, error FROM active_turns WHERE id=?", (identity.turn_id,),
    ).fetchone()
    assert turn["status"] == "error"
    assert turn["error"] == "unexpected exit"


@pytest.mark.asyncio
async def test_pending_exit_cannot_finalize_a_successor():
    from core.turn_core import TurnIdentity
    from subagents import cc_runtime as runtime
    from subagents import cc_runtime_lifecycle as lifecycle
    from subagents.cc_runtime_state import PendingProcessExit

    thread_id = "cc-exit-successor-fence"
    old_identity = TurnIdentity("old-turn", 1)
    old = runtime._Live(
        session=SimpleNamespace(),
        parent_thread_id=None,
        name="old",
        corsia_turn=old_identity,
        turn_active=True,
        pending_process_exit=PendingProcessExit(
            old_identity, 7, "old exact stderr",
        ),
    )
    successor = runtime._Live(
        session=SimpleNamespace(),
        parent_thread_id=None,
        name="successor",
        corsia_turn=TurnIdentity("new-turn", 2),
        turn_active=True,
    )
    runtime._LIVE.put(thread_id, successor)
    try:
        assert await lifecycle.finalize_pending_process_exit(
            thread_id, old, lambda *_args: None,
        ) is False
        assert runtime._LIVE.get(thread_id) is successor
        assert old.pending_process_exit is not None
    finally:
        runtime._LIVE.pop(thread_id, None)


@pytest.mark.asyncio
async def test_resolved_natural_exit_already_removed_by_callback_is_settled():
    from subagents import cc_runtime as runtime
    from subagents import cc_runtime_lifecycle as lifecycle

    thread_id = "cc-exit-already-finalized"
    live = runtime._Live(
        session=SimpleNamespace(),
        parent_thread_id=None,
        name="cc",
        process_exit_resolved=True,
    )
    finalizer = AsyncMock()

    assert await lifecycle.settle_and_finalize_retired_live(
        thread_id,
        live,
        lambda *_args: None,
        emit_closed=True,
        finalize_callback=finalizer,
    ) is True
    finalizer.assert_not_awaited()


@pytest.mark.asyncio
async def test_exit_settlement_failure_retains_exact_outcome_for_close_retry(
    main_db, tmp_path, monkeypatch,
):
    from subagents import cc_runtime as runtime
    from subagents import cc_runtime_lifecycle as lifecycle
    from subagents import cc_runtime_resume as resume
    from subagents import cc_destination, cc_event_tool_settlement, cc_startup_process
    from subagents import cc_runtime_helpers as runtime_helpers
    from subagents.child_thread import persist_assistant_step

    class ExitableSession(StubCCInvocation):
        returncode = 7

        def __init__(self, **kwargs):
            self.mcp_config = kwargs["mcp_config"]
            self.on_exit = kwargs["on_exit"]

        async def start(self):
            return None

        async def close(self):
            return self.returncode

        async def terminate(self):
            return None

    monkeypatch.setattr(cc_startup_process, "CCStreamSession", ExitableSession)
    monkeypatch.setattr(runtime_helpers, "_validate_working_dir", lambda *_args: None)
    monkeypatch.setattr(cc_destination, "_main_working_dir", lambda *_args: str(tmp_path))
    broadcasts: list[dict] = []
    bcast = lambda _sid, payload: broadcasts.append(payload)
    target = cc_target()
    prepared = await prepared_cc_input("main-1", "", target)
    live = await _start_under_lock(
        resume, runtime, "main-1",
        target=target,
        binary="claude",
        claude_dir=str(tmp_path / "claude-home"),
        broadcast=bcast,
        is_subagent=False,
        current_user_text="",
        prepared_context=prepared,
        surface_exit_error_fn=runtime._finish_process_exit,
        finalize_fn=runtime._finalize,
    )
    assert live is not None
    identity = _admit_active_turn(main_db, "cc-exit-retry-turn")
    step_id = persist_assistant_step(
        "main-1",
        content="",
        tool_uses=[{"id": "call-1", "name": "Read", "arguments": {}}],
        turn_id=identity.turn_id,
        db=main_db,
    )
    live.corsia_turn = identity
    live.turn_active = True
    live.tool_step_ids["call-1"] = step_id
    live.tool_names["call-1"] = "Read"
    live.tools_settled.clear()
    main_db.commit()

    original_persist = cc_event_tool_settlement.persist_tool_result
    fail_once = True

    def flaky_persist(*args, **kwargs):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise RuntimeError("first durable tool result failed")
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(cc_event_tool_settlement, "persist_tool_result", flaky_persist)

    with pytest.raises(RuntimeError, match="first durable tool result failed"):
        await live.session.on_exit(7, "exact natural stderr")

    assert runtime._LIVE.get("main-1") is live
    assert live.turn_active is True
    assert live.pending_process_exit is not None
    assert live.pending_process_exit.identity == identity
    assert live.pending_process_exit.code == 7
    assert live.pending_process_exit.stderr == "exact natural stderr"

    assert await lifecycle.close_and_finalize("main-1", live, bcast) is True

    turn = main_db.execute(
        "SELECT status, error FROM active_turns WHERE id=?", (identity.turn_id,),
    ).fetchone()
    tool_rows = main_db.execute(
        "SELECT metadata FROM messages WHERE thread_id='main-1' AND role='tool'",
    ).fetchall()
    assert turn["status"] == "error"
    assert turn["error"] == "exact natural stderr"
    assert len(tool_rows) == 1
    assert runtime._LIVE.get("main-1") is None
    assert live.pending_process_exit is None


@pytest.mark.asyncio
async def test_constructor_failure_cleans_unregistered_seed_and_config(
    main_db, tmp_path, monkeypatch,
):
    from subagents import cc_runtime as runtime
    from subagents import cc_destination, cc_startup_process
    from subagents import cc_runtime_helpers as runtime_helpers

    created: dict[str, str] = {}

    def fail_constructor(**kwargs):
        created["config"] = kwargs["mcp_config"]
        raise RuntimeError("constructor broke")

    monkeypatch.setattr(cc_startup_process, "CCStreamSession", fail_constructor)
    monkeypatch.setattr(runtime_helpers, "_validate_working_dir", lambda *_args: None)
    monkeypatch.setattr(cc_destination, "_main_working_dir", lambda *_args: str(tmp_path))

    seed_glob = "cc-seed-main-1-*"
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    seeds_before = set(tmp_path.glob(seed_glob))
    target = cc_target()
    prepared = await prepared_cc_input("main-1", "hello", target)
    with pytest.raises(RuntimeError, match="constructor broke"):
        await runtime.feed_main(
            "main-1", "hello", target=target, binary="claude",
            turn_id="turn-main-1", claude_dir=str(tmp_path / "claude-home"),
            prepared_context=prepared,
        )

    assert runtime._LIVE.get("main-1") is None
    assert not Path(created["config"]).exists()
    assert set(Path(tempfile.gettempdir()).glob(seed_glob)) == seeds_before


@pytest.mark.asyncio
async def test_config_failure_cleans_unregistered_seed(main_db, tmp_path, monkeypatch):
    from subagents import cc_runtime as runtime
    from subagents import cc_destination, cc_startup_context
    from subagents import cc_runtime_helpers as runtime_helpers
    from subagents import harness_tool_bridge

    seed = tmp_path / "seed.txt"

    def write_seed(*_args):
        seed.write_text("seed", encoding="utf-8")
        return str(seed)

    def fail_configure(*_args, **_kwargs):
        raise RuntimeError("config broke")

    monkeypatch.setattr(cc_startup_context, "_write_seed_file", write_seed)
    monkeypatch.setattr(runtime_helpers, "_validate_working_dir", lambda *_args: None)
    monkeypatch.setattr(cc_destination, "_main_working_dir", lambda *_args: str(tmp_path))
    monkeypatch.setattr(harness_tool_bridge, "configure_cc_tool_bridge", fail_configure)

    target = cc_target()
    prepared = await prepared_cc_input("main-1", "hello", target)
    with pytest.raises(RuntimeError, match="config broke"):
        await runtime.feed_main(
            "main-1", "hello", target=target, binary="claude",
            turn_id="turn-main-1", claude_dir=str(tmp_path / "claude-home"),
            prepared_context=prepared,
        )

    assert runtime._LIVE.get("main-1") is None
    assert not seed.exists()


@pytest.mark.asyncio
async def test_close_failure_revokes_and_finalizes_exact_live(tmp_path):
    from subagents.cc_handle_close import close_handle
    from subagents.cc_runtime_state import _LIVE, _Live

    config = tmp_path / "mcp.json"
    config.write_text("secret", encoding="utf-8")

    class FailingClose:
        mcp_config = str(config)
        returncode = 1

        async def close(self):
            raise RuntimeError("close broke")

    live = _Live(
        session=FailingClose(),
        seed_file="",
        parent_thread_id=None,
        name="cc",
        tool_config=str(config),
        tool_registry=object(),
        tool_token="live-secret",
        provider_context_untrusted=False,
    )
    _LIVE.put("thread-1", live)

    with pytest.raises(RuntimeError, match="close broke"):
        await close_handle("thread-1", live, None)

    assert _LIVE.get("thread-1") is None
    assert live.tool_registry is None
    assert live.tool_token == ""
    assert not config.exists()


@pytest.mark.asyncio
async def test_start_failure_cleanup_survives_terminate_failure(
    child_db, tmp_path, monkeypatch,
):
    from subagents import cc_runtime as runtime
    from subagents import cc_spawn
    from subagents import harness_tool_bridge

    config = tmp_path / "spawn-mcp.json"

    def write_config(**_kwargs):
        config.write_text("secret", encoding="utf-8")
        return str(config)

    class FailingSession(StubCCInvocation):
        returncode = None

        def __init__(self, **kwargs):
            self.mcp_config = kwargs["mcp_config"]

        async def start(self):
            raise RuntimeError("start broke")

        async def terminate(self):
            raise RuntimeError("terminate broke")

    monkeypatch.setattr(cc_spawn, "CCStreamSession", FailingSession)
    monkeypatch.setattr(harness_tool_bridge, "write_cc_mcp_config", write_config)
    monkeypatch.setattr(cc_spawn, "_validate_working_dir", lambda *_args: None)

    _seed_parent(child_db)
    try:
        with pytest.raises(RuntimeError, match="terminate broke"):
            await runtime.spawn(
                "parent", "task", binary="claude",
                target=cc_target("sonnet"),
                broadcast=lambda *_args: None,
            )
        assert not config.exists()
        child_ids = runtime._LIVE.ids()
        assert len(child_ids) == 1
        live = runtime._LIVE.get(child_ids[0])
        assert live.provider_context_untrusted is True
        assert live.tool_registry is None
        assert live.tool_token == ""
    finally:
        runtime._LIVE.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["error", "cancel"])
async def test_spawn_post_write_failure_terminalizes_before_releasing_owner(
    child_db, tmp_path, monkeypatch, failure_kind,
):
    from subagents import cc_runtime as runtime
    from subagents import cc_spawn

    class PostWriteFailureSession(StubCCInvocation):
        returncode = None

        def __init__(self, **kwargs):
            self.mcp_config = kwargs["mcp_config"]

        async def start(self):
            return None

        async def feed(self, _text, *, on_write_observed, on_accepted, input_id=None):
            on_write_observed()
            if failure_kind == "cancel":
                raise asyncio.CancelledError
            raise RuntimeError("post-write drain failed")

        async def terminate(self):
            self.returncode = -9

    monkeypatch.setattr(cc_spawn, "CCStreamSession", PostWriteFailureSession)
    monkeypatch.setattr(cc_spawn, "_validate_working_dir", lambda *_args: None)
    _seed_parent(child_db)

    expected = asyncio.CancelledError if failure_kind == "cancel" else RuntimeError
    with pytest.raises(expected):
        await runtime.spawn(
            "parent",
            "accepted input",
            binary="claude",
            target=cc_target("sonnet"),
            broadcast=lambda *_args: None,
        )

    turn = child_db.execute(
        "SELECT status FROM active_turns ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    assert turn is not None
    assert turn["status"] == "error"
    input_row = child_db.execute(
        "SELECT delivery_state,write_outcome FROM messages "
        "WHERE role='user' ORDER BY input_order DESC LIMIT 1"
    ).fetchone()
    assert dict(input_row) == {
        "delivery_state": "ambiguous",
        "write_outcome": "ambiguous",
    }
    assert runtime._LIVE.ids() == ()


@pytest.mark.asyncio
async def test_spawn_post_write_cancellation_survives_cleanup_failure(
    child_db, tmp_path, monkeypatch,
):
    from subagents import cc_runtime as runtime
    from subagents import cc_spawn

    class CancelledSession(StubCCInvocation):
        returncode = None

        def __init__(self, **kwargs):
            self.mcp_config = kwargs["mcp_config"]

        async def start(self):
            return None

        async def feed(self, _text, *, on_write_observed, on_accepted, input_id=None):
            on_write_observed()
            raise asyncio.CancelledError

        async def terminate(self):
            raise RuntimeError("cleanup terminate failed")

    monkeypatch.setattr(cc_spawn, "CCStreamSession", CancelledSession)
    monkeypatch.setattr(cc_spawn, "_validate_working_dir", lambda *_args: None)
    _seed_parent(child_db)

    try:
        with pytest.raises(asyncio.CancelledError):
            await runtime.spawn(
                "parent",
                "accepted input",
                binary="claude",
                target=cc_target("sonnet"),
                broadcast=lambda *_args: None,
            )
        child_ids = runtime._LIVE.ids()
        assert len(child_ids) == 1
        live = runtime._LIVE.get(child_ids[0])
        assert live is not None
        assert live.provider_context_untrusted is True
        assert live.turn_active is False
        turn = child_db.execute(
            "SELECT status FROM active_turns ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        assert turn["status"] == "error"
        input_row = child_db.execute(
            "SELECT delivery_state,write_outcome FROM messages "
            "WHERE role='user' ORDER BY input_order DESC LIMIT 1"
        ).fetchone()
        assert dict(input_row) == {
            "delivery_state": "ambiguous",
            "write_outcome": "ambiguous",
        }
    finally:
        runtime._LIVE.reset()


@pytest.mark.asyncio
async def test_spawn_natural_exit_settlement_failure_retries_exact_error(
    child_db, tmp_path, monkeypatch,
):
    from subagents import cc_runtime as runtime
    from subagents import cc_event_tool_settlement, cc_spawn
    from subagents import cc_runtime_lifecycle as lifecycle
    from subagents.child_thread import persist_assistant_step

    class ExitableSpawnSession(StubCCInvocation):
        returncode = None

        def __init__(self, **kwargs):
            self.mcp_config = kwargs["mcp_config"]
            self.on_exit = kwargs["on_exit"]

        async def start(self):
            return None

        async def feed(self, _text, *, on_write_observed, on_accepted, input_id=None):
            on_write_observed()
            on_accepted()

        async def close(self):
            self.returncode = 7
            return self.returncode

        async def terminate(self):
            self.returncode = -9

    monkeypatch.setattr(cc_spawn, "CCStreamSession", ExitableSpawnSession)
    monkeypatch.setattr(cc_spawn, "_validate_working_dir", lambda *_args: None)
    _seed_parent(child_db)
    bcast = lambda *_args: None
    child_id = await runtime.spawn(
        "parent",
        "accepted input",
        binary="claude",
        target=cc_target("sonnet"),
        broadcast=bcast,
    )
    live = runtime._LIVE.get(child_id)
    assert live is not None and live.corsia_turn is not None
    identity = live.corsia_turn
    live.apply_now_interrupt_turn = identity
    step_id = persist_assistant_step(
        child_id,
        content="",
        tool_uses=[{"id": "spawn-call", "name": "Read", "arguments": {}}],
        turn_id=identity.turn_id,
        db=child_db,
    )
    live.tool_step_ids["spawn-call"] = step_id
    live.tool_names["spawn-call"] = "Read"
    live.tools_settled.clear()
    child_db.commit()

    original_persist = cc_event_tool_settlement.persist_tool_result
    fail_once = True

    def flaky_persist(*args, **kwargs):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise RuntimeError("spawn tool result failed")
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(cc_event_tool_settlement, "persist_tool_result", flaky_persist)

    with pytest.raises(RuntimeError, match="spawn tool result failed"):
        await live.session.on_exit(7, "spawn exact stderr")
    assert runtime._LIVE.get(child_id) is live
    assert live.pending_process_exit is not None
    assert live.apply_now_interrupt_turn == identity

    assert await lifecycle.retire_live(child_id, bcast) is True
    assert live.apply_now_interrupt_turn is None
    turn = child_db.execute(
        "SELECT status, error FROM active_turns WHERE id=?", (identity.turn_id,),
    ).fetchone()
    assert turn["status"] == "error"
    assert turn["error"] == "spawn exact stderr"
    assert runtime._LIVE.get(child_id) is None
