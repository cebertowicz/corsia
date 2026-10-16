"""MCP reload serializes with start and stop, and an import's effect on live tool sets is fixed."""
from __future__ import annotations

import asyncio
import json

import pytest

from tools.base import Tool


class _FakeTool(Tool):
    user_configured = True
    always_advertised = True

    def __init__(self, name: str) -> None:
        self._name = name
        self.session_open = True

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} fixture"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> str:
        return "ok"


@pytest.fixture
def runtime(monkeypatch):
    from infra import state
    from tools import mcp_runtime
    from tools.registry import ToolRegistry

    monkeypatch.setattr(mcp_runtime, "_stack", None)
    monkeypatch.setattr(mcp_runtime, "_stopped", False)
    monkeypatch.setattr(mcp_runtime, "_lock", asyncio.Lock())
    monkeypatch.setattr(mcp_runtime, "_registry", ToolRegistry())
    monkeypatch.setattr(mcp_runtime, "_connected", set())
    monkeypatch.setattr(mcp_runtime, "_app_stacks", {})
    monkeypatch.setattr(mcp_runtime, "_app_tools", {})
    monkeypatch.setattr(mcp_runtime, "_app_servers", {})
    monkeypatch.setattr(mcp_runtime, "_app_states", {})
    monkeypatch.setattr(mcp_runtime, "_reconnect_at", {})
    monkeypatch.setattr(state, "_agent", None)
    tools: list[_FakeTool] = []
    gate = {"hold": None}

    async def fake_connect(config, registry, stack):
        if gate["hold"] is not None:
            gate["entered"].set()
            await gate["hold"].wait()
        registry.mcp_server_configs = {**getattr(registry, "mcp_server_configs", {}), **config}
        for name, server in config.items():
            suffix = server.get("version", "tool")
            tool = _FakeTool(f"{name}_{suffix}")
            tool.mcp_app_id = server.get("connected_app_id")
            tools.append(tool)
            registry.register(tool)

            async def close(tool=tool):
                tool.session_open = False

            stack.push_async_callback(close)
        return {}

    monkeypatch.setattr(mcp_runtime, "connect_mcp_servers", fake_connect)
    return mcp_runtime, tools, gate


def test_import_racing_shutdown_leaves_no_open_sessions(runtime):
    mcp_runtime, tools, gate = runtime

    async def scenario():
        await mcp_runtime.start_mcp_runtime({"old": {}})
        gate["hold"], gate["entered"] = asyncio.Event(), asyncio.Event()
        reload_task = asyncio.create_task(mcp_runtime.reload_mcp_runtime({"old": {}, "new": {}}))
        await gate["entered"].wait()
        stop_task = asyncio.create_task(mcp_runtime.stop_mcp_runtime())
        await asyncio.sleep(0)
        assert not stop_task.done()  # stop waits for the reload instead of racing it
        gate["hold"].set()
        assert await reload_task == {}
        await stop_task

    asyncio.run(scenario())

    assert {tool.name for tool in tools} == {"old_tool", "new_tool"}
    assert not any(tool.session_open for tool in tools)
    assert mcp_runtime._stack is None


def test_reload_after_stop_opens_nothing(runtime):
    mcp_runtime, tools, _ = runtime

    async def scenario():
        await mcp_runtime.start_mcp_runtime({"old": {}})
        await mcp_runtime.stop_mcp_runtime()
        return await mcp_runtime.reload_mcp_runtime({"old": {}, "new": {}})

    errors = asyncio.run(scenario())

    assert errors == {"old": "MCP runtime is stopped", "new": "MCP runtime is stopped"}
    assert mcp_runtime._stack is None
    assert [tool.name for tool in tools] == ["old_tool"]
    assert not any(tool.session_open for tool in tools)


def test_start_after_stop_runs_again(runtime):
    mcp_runtime, tools, _ = runtime

    async def scenario():
        await mcp_runtime.start_mcp_runtime({})
        await mcp_runtime.stop_mcp_runtime()
        await mcp_runtime.start_mcp_runtime({"again": {}})
        try:
            assert await mcp_runtime.reload_mcp_runtime({"again": {}, "more": {}}) == {}
        finally:
            await mcp_runtime.stop_mcp_runtime()

    asyncio.run(scenario())

    assert [tool.name for tool in tools] == ["again_tool", "more_tool"]
    assert not any(tool.session_open for tool in tools)


def test_import_adds_tools_to_the_native_block_and_it_is_stable_afterwards(runtime, monkeypatch):
    from infra import state
    from tools.catalog import advertised_tool_definitions
    from tools.registry import ToolRegistry

    mcp_runtime, _, _ = runtime

    def block(registry):
        return json.dumps(advertised_tool_definitions(registry), sort_keys=False)

    async def scenario():
        await mcp_runtime.start_mcp_runtime({"old": {}})
        agent = type("Agent", (), {"tools": ToolRegistry()})()
        mcp_runtime.register_mcp_tools(agent.tools)
        monkeypatch.setattr(state, "_agent", agent)
        before = block(agent.tools)
        try:
            await mcp_runtime.reload_mcp_runtime({"old": {}, "zeta": {}, "alpha": {}})
            first, second = block(agent.tools), block(agent.tools)
            await mcp_runtime.reload_mcp_runtime({"old": {}, "zeta": {}, "alpha": {}})
            third = block(agent.tools)
        finally:
            await mcp_runtime.stop_mcp_runtime()
        return agent, before, first, second, third

    agent, before, first, second, third = asyncio.run(scenario())

    assert "alpha_tool" not in before and "alpha_tool" in first and "zeta_tool" in first
    assert first == second == third  # one prefix change at the import, none after
    names = [d["function"]["name"] for d in json.loads(first)]
    assert names == sorted(names)  # order does not depend on registration order


def test_connected_app_reconnect_rate_limit_names_the_ten_second_wait(runtime):
    from tools.mcp_runtime import MCPReconnectRateLimitError

    mcp_runtime, _tools, _gate = runtime
    mcp_runtime._reconnect_at["app-1"] = mcp_runtime.time.monotonic()

    with pytest.raises(MCPReconnectRateLimitError, match="try again in 10 seconds"):
        asyncio.run(mcp_runtime.apply_connected_app_runtime("reconnect", "app-1"))


def test_connected_app_reconnect_and_remove_own_only_their_app_runtime(runtime, monkeypatch):
    from infra import state
    from tools.registry import ToolRegistry

    mcp_runtime, tools, _ = runtime
    versions = {"app-1": 1}

    async def load(fn, *args):
        if fn.__name__ == "load_runtime_app_config":
            app_id = args[0]
            return "GitHub", {"connected_app_id": app_id, "version": versions[app_id]}
        if fn.__name__ == "missing_app_key_refs":
            return []
        if fn.__name__ == "record_runtime_app_state":
            return None
        raise AssertionError(fn.__name__)

    monkeypatch.setattr(mcp_runtime, "run_in_db", load)

    async def scenario():
        await mcp_runtime.start_mcp_runtime({})
        agent = type("Agent", (), {"tools": ToolRegistry()})()
        monkeypatch.setattr(state, "_agent", agent)
        first = await mcp_runtime.apply_connected_app_runtime("connect", "app-1")
        assert first["state"] == "working"
        assert agent.tools.has("GitHub_1")
        versions["app-1"] = 2
        replacement = await mcp_runtime.apply_connected_app_runtime("reconnect", "app-1")
        assert replacement["state"] == "working"
        assert agent.tools.has("GitHub_2")
        assert not agent.tools.has("GitHub_1")
        await mcp_runtime.apply_connected_app_runtime("remove", "app-1")
        assert not agent.tools.has("GitHub_2")
        assert mcp_runtime.connected_app_state("app-1")["state"] == "disconnected"
        assert mcp_runtime._app_tools == {}
        assert mcp_runtime._app_servers == {}
        assert getattr(mcp_runtime._registry, "mcp_server_configs", {}) == {}
        await mcp_runtime.stop_mcp_runtime()

    asyncio.run(scenario())

    assert [tool.name for tool in tools] == ["GitHub_1", "GitHub_2"]
    assert not any(tool.session_open for tool in tools)


def test_connected_app_with_an_unset_key_never_attempts_a_connection(runtime, monkeypatch):
    mcp_runtime, _tools, _gate = runtime
    published = []

    async def load(fn, *args):
        if fn.__name__ == "load_runtime_app_config":
            return "GitHub", {"connected_app_id": args[0], "secret_env": ["GITHUB_TOKEN"]}
        if fn.__name__ == "missing_app_key_refs":
            return ["GITHUB_TOKEN"]
        if fn.__name__ == "record_runtime_app_state":
            published.append(args[1])
            return None
        raise AssertionError(fn.__name__)

    monkeypatch.setattr(mcp_runtime, "run_in_db", load)

    result = asyncio.run(mcp_runtime.apply_connected_app_runtime("connect", "app-1"))

    assert result["state"] == "needs_token"
    assert [state["state"] for state in published] == ["connecting", "needs_token"]
    assert mcp_runtime._app_stacks == {}


def test_connected_app_keeps_the_transport_failure_class(runtime, monkeypatch):
    from tools.mcp import MCPConnectionFailure

    mcp_runtime, _tools, _gate = runtime
    published = []

    async def load(fn, *args):
        if fn.__name__ == "load_runtime_app_config":
            return "GitHub", {"connected_app_id": args[0]}
        if fn.__name__ == "missing_app_key_refs":
            return []
        if fn.__name__ == "record_runtime_app_state":
            published.append(args[1])
            return None
        raise AssertionError(fn.__name__)

    async def failed_connect(config, _registry, _stack):
        return {next(iter(config)): MCPConnectionFailure("timed out", "timed_out")}

    monkeypatch.setattr(mcp_runtime, "run_in_db", load)
    monkeypatch.setattr(mcp_runtime, "connect_mcp_servers", failed_connect)

    result = asyncio.run(mcp_runtime.apply_connected_app_runtime("connect", "app-1"))

    assert result == {
        "state": "failed", "state_since": result["state_since"], "tool_count": 0,
        "failure_class": "timed_out",
    }
    assert [state["state"] for state in published] == ["connecting", "failed"]
