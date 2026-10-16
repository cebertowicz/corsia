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
    monkeypatch.setattr(mcp_runtime, "_connection_started_at", {})
    monkeypatch.setattr(state, "_agent", None)
    tools: list[_FakeTool] = []
    gate = {"hold": None}

    async def fake_connect(config, registry, stack):
        if gate["hold"] is not None:
            gate["entered"].set()
            await gate["hold"].wait()
        for name in config:
            tool = _FakeTool(f"{name}_tool")
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


def test_connection_timestamp_tracks_successful_sessions_and_clears_on_restart(runtime, monkeypatch):
    mcp_runtime, _, _ = runtime
    times = iter(("2030-01-01T00:00:00", "2030-01-01T00:00:01", "2030-01-01T00:00:02"))
    monkeypatch.setattr(mcp_runtime, "utc_now_iso", lambda: next(times))

    async def scenario():
        await mcp_runtime.start_mcp_runtime({"first": {}})
        first = mcp_runtime.connection_started_at("first")
        await mcp_runtime.reload_mcp_runtime({"first": {}, "second": {}})
        second = mcp_runtime.connection_started_at("second")
        assert mcp_runtime.connection_started_at("first") == first
        await mcp_runtime.stop_mcp_runtime()
        assert mcp_runtime.connection_started_at("first") is None
        await mcp_runtime.start_mcp_runtime({"first": {}})
        restarted = mcp_runtime.connection_started_at("first")
        await mcp_runtime.stop_mcp_runtime()
        return first, second, restarted

    first, second, restarted = asyncio.run(scenario())
    assert first == "2030-01-01T00:00:00"
    assert second == "2030-01-01T00:00:01"
    assert restarted == "2030-01-01T00:00:02"


def test_connection_timestamp_is_visible_while_startup_connects_and_clears_when_stopped(runtime):
    mcp_runtime, _, gate = runtime
    gate["hold"], gate["entered"] = asyncio.Event(), asyncio.Event()

    async def scenario():
        task = asyncio.create_task(mcp_runtime.start_mcp_runtime({"waiting": {}}))
        await gate["entered"].wait()
        attempted_at = mcp_runtime.connection_started_at("waiting")
        assert attempted_at is not None
        gate["hold"].set()
        await task
        assert mcp_runtime.connection_started_at("waiting") == attempted_at
        await mcp_runtime.stop_mcp_runtime()
        assert mcp_runtime.connection_started_at("waiting") is None

    asyncio.run(scenario())


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
