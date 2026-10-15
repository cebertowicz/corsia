"""Exercise the installed MCP SDK over real local Streamable HTTP."""

import asyncio
from contextlib import AsyncExitStack
import json
import logging
from pathlib import Path
import socket
import sqlite3
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
import uvicorn

from tools.mcp import connect_mcp_servers
from tools.registry import ToolRegistry


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
    from tools.mcp_runtime import start_mcp_runtime, stop_mcp_runtime

    # Preserve any session-scoped integration app's runtime while this test
    # exercises independent startup/shutdown cycles on its own event loop.
    monkeypatch.setattr("tools.mcp_runtime._stack", None)
    monkeypatch.setattr("tools.mcp_runtime._stopped", False)
    monkeypatch.setattr("tools.mcp_runtime._lock", asyncio.Lock())
    monkeypatch.setattr("tools.mcp_runtime._registry", ToolRegistry())

    sizes = [8, 7, 3, 4, 7, 7, 3]
    http_mcp.names = [f"read_{index}" for index in range(8)] + ["write_record"]
    config = server_config(tmp_path, http_mcp)
    servers = {f"service_{index}": {**config, "tool_prefix": f"service_{index}_",
                "allowed_tools": [f"read_{item}" for item in range(count)], "always_load": True}
               for index, count in enumerate(sizes)}
    # A restart discards the old sessions and discovers them anew. New registry
    # builds include them before preflight/context accounting in both runs.
    for _ in range(2):
        try:
            assert await start_mcp_runtime(servers) == {}
            native = CORSIAAgent(tmp_path)
            await native._connect_mcp()
            for _ in range(2):
                registry = build_tool_registry(str(tmp_path))
                definitions = dynamic_tool_definitions(registry)
                names = {item["function"]["name"] for item in definitions
                         if item["function"]["name"].startswith("service_")}
                assert len(names) == 39
                assert names == set(native.tools.tool_names)
                for index in range(7):
                    assert await registry.get(f"service_{index}_read_0").execute() == "ok"
                assert http_mcp.token not in json.dumps(definitions)
        finally:
            await stop_mcp_runtime()
    assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("mcp:")]
