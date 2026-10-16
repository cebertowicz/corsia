"""Exercise the installed MCP SDK over real local Streamable HTTP."""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import json
import logging
from pathlib import Path
import socket
import sqlite3
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route
import uvicorn

from tools.mcp import connect_mcp_servers
from tools.registry import ToolRegistry


@asynccontextmanager
async def _serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(3):
            while not server.started:
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 3)
        sock.close()


class MCPServer:
    def __init__(self):
        self.headers = []
        self.calls = []
        self.token = "synthetic-mcp-bearer"
        self.names = ["read_profile", "delete_record"]
        self.status = 200
        self.delay = 0
        self.echo = False
        self.instructions = None

    async def respond(self, request: Request):
        authorization = request.headers.get("authorization")
        self.headers.append(authorization)
        if authorization != f"Bearer {self.token}" or self.status != 200:
            return JSONResponse({"error": authorization}, status_code=401)
        message = await request.json()
        if "id" not in message:
            return Response(status_code=202)
        await asyncio.sleep(self.delay)
        method = message["method"]
        result: dict[str, Any]
        if method == "initialize":
            result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fixture", "version": "1"}}
            if self.instructions is not None:
                result["instructions"] = self.instructions
        elif method == "tools/list":
            result = {"tools": [{"name": name, "description": "Read fixture data",
                                 "inputSchema": {"type": "object", "properties": {}}}
                                for name in self.names]}
        elif method == "tools/call":
            self.calls.append(message["params"])
            result = {"content": [{"type": "text", "text": self.token if self.echo else "ok"}]}
        else:
            raise AssertionError(method)
        return JSONResponse({"jsonrpc": "2.0", "id": message["id"], "result": result})


class LegacySSEServer:
    """Legacy SSE endpoint paired with a streamable-HTTP refusal."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.headers: list[str | None] = []
        self.http_attempts = 0
        self.events: asyncio.Queue[bytes] = asyncio.Queue()
        self.token = "stored-key-for-sse-fixture"

    async def mcp(self, request: Request):
        if request.method == "POST":
            self.http_attempts += 1
            return Response(status_code=self.status)
        self.headers.append(request.headers.get("authorization"))

        async def event_stream():
            yield b"event: endpoint\ndata: /messages\n\n"
            while True:
                yield await self.events.get()

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    async def messages(self, request: Request):
        self.headers.append(request.headers.get("authorization"))
        message = await request.json()
        if "id" in message:
            result: dict[str, object]
            if message["method"] == "initialize":
                result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "legacy", "version": "1"}}
            elif message["method"] == "tools/list":
                result = {"tools": [{"name": "read_profile", "description": "Read fixture data",
                                     "inputSchema": {"type": "object", "properties": {}}}]}
            elif message["method"] == "tools/call":
                result = {"content": [{"type": "text", "text": "ok"}]}
            else:
                raise AssertionError(message["method"])
            await self.events.put((
                "event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result})
                + "\n\n"
            ).encode())
        return Response(status_code=202)


@pytest.fixture
async def http_mcp():
    endpoint = MCPServer()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    endpoint.url = f"http://127.0.0.1:{sock.getsockname()[1]}/mcp"
    server = uvicorn.Server(uvicorn.Config(
        Starlette(routes=[Route("/mcp", endpoint.respond, methods=["POST"])]),
        log_level="error", lifespan="off",
    ))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(3):
            while not server.started:
                await asyncio.sleep(0.01)
        yield endpoint
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 3)
        sock.close()


def server_config(tmp_path, endpoint, **overrides):
    bearer = tmp_path / "bearer"
    bearer.write_text(endpoint.token)
    bearer.chmod(0o600)
    return {"url": endpoint.url, "bearer_file": str(bearer),
            "allowed_tools": ["read_profile"], "tool_prefix": "work_",
            **overrides}


@pytest.mark.asyncio
async def test_http_bearer_allowlist_and_identity(tmp_path, http_mcp):
    config = server_config(tmp_path, http_mcp)
    registry = ToolRegistry()
    async with AsyncExitStack() as stack:
        await connect_mcp_servers({"fixture": config}, registry, stack)
        assert registry.tool_names == ["work_read_profile"]
        tool = registry.get("work_read_profile")
        assert await tool.execute(_thread_id="secret-context") == "ok"
        assert http_mcp.calls == [{"name": "read_profile", "arguments": {}}]
        assert set(http_mcp.headers) == {f"Bearer {http_mcp.token}"}
        assert http_mcp.token not in json.dumps(registry.get_definitions())
        assert http_mcp.token not in json.dumps(config)


@pytest.mark.asyncio
async def test_stored_header_key_is_not_sent_to_a_redirect_target(isolated_db, caplog):
    from infra import key_store
    from tools import mcp_secrets

    origin_headers: list[str | None] = []
    redirected_headers: list[str | None] = []
    stored_key = "Bearer_redirect_key_for_fixture"

    async def redirected(request: Request):
        redirected_headers.append(request.headers.get("authorization"))
        return Response(status_code=500)

    async with _serve(Starlette(routes=[Route("/mcp", redirected, methods=["POST"])])) as redirected_url:
        async def redirecting(request: Request):
            origin_headers.append(request.headers.get("authorization"))
            return RedirectResponse(f"{redirected_url}/mcp", status_code=307)

        async with _serve(Starlette(routes=[Route("/mcp", redirecting, methods=["POST"])])) as origin_url:
            key_store.set_managed_value(
                mcp_secrets.entry_name("redirect", "headers", "Authorization"), stored_key,
            )
            config = {
                "url": f"{origin_url}/mcp", "secret_headers": ["Authorization"],
                "allowed_tools": ["read_profile"], "connect_timeout": 1,
            }
            registry = ToolRegistry()
            async with AsyncExitStack() as stack:
                errors = await connect_mcp_servers({"redirect": config}, registry, stack)

    assert errors["redirect"]
    assert registry.tool_names == []
    assert origin_headers == [stored_key]
    assert redirected_headers == []
    assert stored_key not in caplog.text


@pytest.mark.asyncio
async def test_sse_endpoint_on_another_origin_never_receives_stored_key(isolated_db, caplog):
    from infra import key_store
    from tools import mcp_secrets

    origin_headers: list[str | None] = []
    redirected_headers: list[str | None] = []
    stored_key = "Bearer_cross_origin_key_for_fixture"

    async def redirected(request: Request):
        redirected_headers.append(request.headers.get("authorization"))
        return Response(status_code=500)

    async with _serve(Starlette(routes=[Route("/messages", redirected, methods=["POST"])])) as redirected_url:
        async def sse(request: Request):
            origin_headers.append(request.headers.get("authorization"))

            async def event_stream():
                yield f"event: endpoint\ndata: {redirected_url}/messages\n\n".encode()

            return StreamingResponse(event_stream(), media_type="text/event-stream")

        async with _serve(Starlette(routes=[Route("/mcp", sse, methods=["GET"])])) as origin_url:
            key_store.set_managed_value(
                mcp_secrets.entry_name("cross-origin", "headers", "Authorization"), stored_key,
            )
            config = {
                "url": f"{origin_url}/mcp", "transport": "sse", "secret_headers": ["Authorization"],
                "allowed_tools": ["read_profile"], "connect_timeout": 1,
            }
            registry = ToolRegistry()
            async with AsyncExitStack() as stack:
                errors = await connect_mcp_servers({"cross-origin": config}, registry, stack)

    assert errors["cross-origin"]
    assert registry.tool_names == []
    assert origin_headers == [stored_key]
    assert redirected_headers == []
    assert stored_key not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
async def test_sse_redirect_get_never_forwards_stored_api_key(isolated_db, fallback):
    from infra import key_store
    from tools import mcp_secrets

    origin_headers: list[str | None] = []
    redirected_headers: list[str | None] = []

    async def redirected(request: Request):
        redirected_headers.append(request.headers.get("x-api-key"))
        return Response(status_code=500)

    async with _serve(Starlette(routes=[Route("/mcp", redirected, methods=["GET"])])) as redirected_url:
        async def origin(request: Request):
            if fallback and request.method == "POST":
                return Response(status_code=405)
            origin_headers.append(request.headers.get("x-api-key"))
            return RedirectResponse(f"{redirected_url}/mcp", status_code=307)

        async with _serve(Starlette(routes=[Route("/mcp", origin, methods=["GET", "POST"])])) as origin_url:
            key_store.set_managed_value(
                mcp_secrets.entry_name("redirect", "headers", "X-API-Key"), "fixture-api-key",
            )
            config = {
                "url": f"{origin_url}/mcp", "transport": "http" if fallback else "sse",
                "secret_headers": ["X-API-Key"], "connect_timeout": 1,
            }
            registry = ToolRegistry()
            async with AsyncExitStack() as stack:
                errors = await connect_mcp_servers({"redirect": config}, registry, stack)

    assert errors["redirect"]
    assert origin_headers == ["fixture-api-key"]
    assert redirected_headers == []


@pytest.mark.asyncio
async def test_declared_sse_connects_and_executes_tools(tmp_path):
    legacy = LegacySSEServer(405)
    app = Starlette(routes=[
        Route("/mcp", legacy.mcp, methods=["GET", "POST"]),
        Route("/messages", legacy.messages, methods=["POST"]),
    ])
    async with _serve(app) as base_url:
        legacy.url = f"{base_url}/mcp"
        config = server_config(tmp_path, legacy, transport="sse", connect_timeout=1)
        registry = ToolRegistry()
        async with AsyncExitStack() as stack:
            assert await connect_mcp_servers({"legacy": config}, registry, stack) == {}
            assert registry.tool_names == ["work_read_profile"]
            assert await registry.get("work_read_profile").execute() == "ok"

    assert legacy.http_attempts == 0
    assert set(legacy.headers) == {f"Bearer {legacy.token}"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 405])
async def test_http_initialize_404_or_405_falls_back_to_sse(tmp_path, status):
    legacy = LegacySSEServer(status)
    app = Starlette(routes=[
        Route("/mcp", legacy.mcp, methods=["GET", "POST"]),
        Route("/messages", legacy.messages, methods=["POST"]),
    ])
    async with _serve(app) as base_url:
        legacy.url = f"{base_url}/mcp"
        config = server_config(tmp_path, legacy, connect_timeout=1)
        registry = ToolRegistry()
        async with AsyncExitStack() as stack:
            assert await connect_mcp_servers({"legacy": config}, registry, stack) == {}
            assert registry.tool_names == ["work_read_profile"]
            assert await registry.get("work_read_profile").execute() == "ok"

    assert legacy.http_attempts == 1
    assert set(legacy.headers) == {f"Bearer {legacy.token}"}


@pytest.mark.asyncio
async def test_initialize_instructions_reach_every_tool_of_the_server(tmp_path, http_mcp):
    http_mcp.instructions = "Profiles for the fixture service. Cite the record id."
    registry = ToolRegistry()
    async with AsyncExitStack() as stack:
        await connect_mcp_servers({"fixture": server_config(tmp_path, http_mcp)}, registry, stack)
        tools = [registry.get(name) for name in registry.tool_names]

    assert {tool.mcp_instructions for tool in tools} == {http_mcp.instructions}
    assert [tool.mcp_tool_name for tool in tools] == ["read_profile"]


@pytest.mark.asyncio
async def test_a_server_without_instructions_reports_none(tmp_path, http_mcp):
    registry = ToolRegistry()
    async with AsyncExitStack() as stack:
        await connect_mcp_servers({"fixture": server_config(tmp_path, http_mcp)}, registry, stack)

        assert registry.get("work_read_profile").mcp_instructions is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [0o600, 0o400])
async def test_private_bearer_modes(tmp_path, http_mcp, mode):
    config = server_config(tmp_path, http_mcp)
    Path(config["bearer_file"]).chmod(mode)
    async with AsyncExitStack() as stack:
        registry = ToolRegistry()
        assert await connect_mcp_servers({"fixture": config}, registry, stack) == {}
        assert await registry.get("work_read_profile").execute() == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o700, 0o000])
async def test_unsafe_bearer_modes_refused_before_http(tmp_path, http_mcp, mode):
    config = server_config(tmp_path, http_mcp)
    Path(config["bearer_file"]).chmod(mode)
    async with AsyncExitStack() as stack:
        registry = ToolRegistry()
        errors = await connect_mcp_servers({"fixture": config}, registry, stack)
        assert errors and registry.tool_names == []
        assert http_mcp.headers == []
        assert http_mcp.token not in str(errors)


@pytest.mark.asyncio
async def test_401_isolated_from_next_server_and_redacted(tmp_path, http_mcp, caplog):
    caplog.set_level(logging.DEBUG)
    config = server_config(tmp_path, http_mcp)
    wrong = tmp_path / "wrong-bearer"
    wrong.write_text("synthetic-wrong-token")
    wrong.chmod(0o600)
    async with AsyncExitStack() as stack:
        registry = ToolRegistry()
        errors = await connect_mcp_servers({
            "bad": {**config, "bearer_file": str(wrong)}, "good": config,
        }, registry, stack)
        assert "HTTP 401" in errors["bad"]
        assert await registry.get("work_read_profile").execute() == "ok"
    assert http_mcp.token not in caplog.text
    assert "synthetic-wrong-token" not in caplog.text


@pytest.mark.asyncio
async def test_unreachable_server_does_not_register_tools(tmp_path, http_mcp):
    config = server_config(tmp_path, http_mcp, url="http://127.0.0.1:1/mcp", connect_timeout=0.2)
    async with AsyncExitStack() as stack:
        registry = ToolRegistry()
        errors = await asyncio.wait_for(connect_mcp_servers({"offline": config}, registry, stack), 2)
        # A closed port can either refuse the connect or fail to complete inside
        # the 0.2s budget, and which of the two documented transport outcomes
        # fires depends on runner load. Both are correct here; the invariant this
        # test owns is that an unreachable server registers no tools and records
        # an error. Each outcome's exact wording is pinned deterministically,
        # without sockets or timing, by test_safe_error_classifies_transport_failures.
        assert errors["offline"] in (
            "server unavailable or protocol failed; check the MCP service and configuration",
            "request timed out; check server availability",
        )
        assert not registry.tool_names


def test_runtime_failure_classes_keep_machine_causes_without_values():
    import httpx

    from tools.mcp import mcp_failure_class
    from tools.mcp_config import MCPConfigError

    request = httpx.Request("POST", "http://example.test/mcp")
    rejected = httpx.HTTPStatusError("rejected", request=request, response=httpx.Response(403, request=request))

    assert mcp_failure_class(rejected, {"secret_env": ["TOKEN"]}) == "token_rejected"
    assert mcp_failure_class(rejected, {}) == "protocol_error"
    assert mcp_failure_class(TimeoutError(), {}) == "timed_out"
    assert mcp_failure_class(OSError(), {"command": "fixture"}) == "cannot_start"
    assert mcp_failure_class(MCPConfigError("public tool name collision"), {}) == "name_collision"


def test_safe_error_classifies_transport_failures():
    import httpx

    from tools.mcp_config import MCPConfigError, safe_error

    timeout = "request timed out; check server availability"
    unavailable = "server unavailable or protocol failed; check the MCP service and configuration"
    request = httpx.Request("POST", "http://127.0.0.1:1/mcp")

    assert safe_error(ConnectionRefusedError()) == unavailable
    assert safe_error(TimeoutError()) == timeout
    assert safe_error(httpx.ConnectTimeout("connect", request=request)) == timeout
    assert safe_error(MCPConfigError("public tool name collision")) == "public tool name collision"

    # The MCP client raises transport faults from an anyio task group, so the
    # real connect path reaches safe_error as a (possibly nested) group. The
    # first member decides the classification and nesting is unwrapped fully.
    assert safe_error(BaseExceptionGroup("wrapped", [ConnectionRefusedError()])) == unavailable
    assert safe_error(BaseExceptionGroup("wrapped", [TimeoutError()])) == timeout
    assert safe_error(BaseExceptionGroup(
        "outer", [BaseExceptionGroup("inner", [httpx.ConnectTimeout("t", request=request),
                                               ConnectionRefusedError()])])) == timeout
    assert safe_error(BaseExceptionGroup(
        "outer", [ConnectionRefusedError(),
                  BaseExceptionGroup("inner", [TimeoutError()])])) == unavailable


@pytest.mark.asyncio
async def test_timeout_and_cancellation_clean_connection_tasks(tmp_path, http_mcp):
    http_mcp.delay = 0.5
    config = server_config(tmp_path, http_mcp, connect_timeout=0.05)
    async with AsyncExitStack() as stack:
        errors = await connect_mcp_servers({"slow": config}, ToolRegistry(), stack)
        assert "timed out" in errors["slow"]
        pending = asyncio.create_task(connect_mcp_servers(
            {"cancel": {**config, "connect_timeout": 5}}, ToolRegistry(), stack,
        ))
        await asyncio.sleep(0.03)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("mcp:")]


@pytest.mark.asyncio
async def test_rotation_user_boundary_and_result_redaction(tmp_path, http_mcp, caplog, isolated_db):
    from infra import state

    with sqlite3.connect(isolated_db) as db:
        db.execute("INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                        ("mcp-owner", "owner", "Fixture", "now", "now"))
    caplog.set_level(logging.DEBUG)
    config = server_config(tmp_path, http_mcp, allowed_user_ids=["owner"])
    async with AsyncExitStack() as stack:
        registry = ToolRegistry()
        await connect_mcp_servers({"fixture": config}, registry, stack)
        wrapper = registry.get("work_read_profile")
        assert "not authorized" in await wrapper.execute(_thread_user_id="owner", _thread_id="mcp-owner")
        assert not http_mcp.calls
        http_mcp.token = "synthetic-rotated-token"
        Path(config["bearer_file"]).write_text(http_mcp.token)
        http_mcp.echo = True
        identity = state._current_thread_id.set("mcp-owner")
        try:
            assert await wrapper.execute() == "[REDACTED]"
            assert http_mcp.calls[0]["arguments"] == {}
            Path(config["bearer_file"]).chmod(0o644)
            assert "0600 or 0400" in await wrapper.execute()
        finally:
            state._current_thread_id.reset(identity)
    assert http_mcp.token not in caplog.text


@pytest.mark.asyncio
async def test_exact_allowlist_and_collisions_fail_closed(tmp_path, http_mcp):
    config = server_config(tmp_path, http_mcp)
    async with AsyncExitStack() as stack:
        registry = ToolRegistry()
        errors = await connect_mcp_servers({
            "missing": {**config, "allowed_tools": ["missing"]},
            "builtin": {**config, "public_names": {"read_profile": "plan_read"}},
            "valid": config, "collision": config,
            "empty": {**config, "allowed_tools": []},
        }, registry, stack)
        assert set(errors) == {"missing", "builtin", "collision"}
        assert registry.tool_names == ["work_read_profile"]


@pytest.mark.asyncio
async def test_seven_servers_and_39_tools_in_fresh_codex_registries(
    tmp_path, http_mcp, isolated_db, monkeypatch,
):
    from core.agent import CORSIAAgent
    from subagents.harness_tool_bridge import build_tool_registry, dynamic_tool_definitions
    from tools import mcp_runtime

    monkeypatch.setattr(mcp_runtime, "_stopped", False)
    monkeypatch.setattr(mcp_runtime, "_lock", asyncio.Lock())
    monkeypatch.setattr(mcp_runtime, "_registry", ToolRegistry())
    monkeypatch.setattr(mcp_runtime, "_app_stacks", {})
    monkeypatch.setattr(mcp_runtime, "_app_tools", {})
    monkeypatch.setattr(mcp_runtime, "_app_servers", {})
    monkeypatch.setattr(mcp_runtime, "_app_states", {})
    monkeypatch.setattr(mcp_runtime, "_reconnect_at", {})
    http_mcp.names = ["read_one", "read_two"]
    first = server_config(tmp_path, http_mcp, allowed_tools=["read_one"], tool_prefix="first_")
    second = server_config(tmp_path, http_mcp, allowed_tools=["read_two"], tool_prefix="second_")
    apps = {
        "app-1": ("First", {**first, "connected_app_id": "app-1"}),
        "app-2": ("Second", {**second, "connected_app_id": "app-2"}),
    }

    async def load(fn, *args):
        if fn.__name__ == "load_runtime_app_ids":
            return list(apps)
        if fn.__name__ == "load_runtime_app_config":
            return apps[args[0]]
        if fn.__name__ == "missing_app_key_refs":
            return []
        if fn.__name__ == "record_runtime_app_state":
            return None
        raise AssertionError(fn.__name__)

    monkeypatch.setattr(mcp_runtime, "run_in_db", load)
    expected = {"first_read_one", "second_read_two"}
    for _ in range(2):
        results = await mcp_runtime.start_connected_app_runtime()
        assert {app_id: result["state"] for app_id, result in results.items()} == {
            "app-1": "working", "app-2": "working",
        }
        native = CORSIAAgent(tmp_path)
        await native._connect_mcp()
        harness = build_tool_registry(str(tmp_path))
        assert set(native.tools.tool_names) == expected
        assert {name for name in harness.tool_names if name in expected} == expected
        assert await native.tools.get("first_read_one").execute() == "ok"
        assert await harness.get("second_read_two").execute() == "ok"
        definitions = dynamic_tool_definitions(harness)
        assert http_mcp.token not in json.dumps(definitions)
        await mcp_runtime.stop_mcp_runtime()

    assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("mcp:")]
