"""A thread that saw a connected app's 401 loads its tools after the key is replaced.

The thread registry a live harness session holds is built once, when that session
starts, so it reads the current connected-app tool set and per-server failure state
instead of holding a copy (issue #2128).
"""
from __future__ import annotations

import asyncio
import sys
import threading
from types import SimpleNamespace

import httpx
import pytest

from mcp import types


class _FakeSession:
    """The one client-session call connect-time discovery makes."""

    def __init__(self, tool_name: str) -> None:
        self._tool_name = tool_name

    async def list_tools(self, cursor=None):
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=self._tool_name,
                    description=f"{self._tool_name} fixture",
                    inputSchema={"type": "object", "properties": {}},
                ),
            ],
        )

    async def call_tool(self, name, arguments=None):
        return types.CallToolResult(content=[types.TextContent(type="text", text="ok")])


def _app_config(app_id: str) -> dict:
    return {
        "url": f"https://{app_id}.invalid/mcp",
        "connected_app_id": app_id,
        "secret_headers": ["X-Api-Key"],
    }


@pytest.fixture
def apps():
    """The connected apps the runtime can load; a test may add to them."""
    return {"app-1": ("Wiki", _app_config("app-1"))}


@pytest.fixture
def app_runtime(monkeypatch, apps):
    """Scratch connected apps over a stubbed streamable-HTTP transport."""
    from infra import state
    from tools import mcp as mcp_module
    from tools import mcp_http_transport, mcp_runtime
    from tools.registry import ToolRegistry

    monkeypatch.setattr(mcp_runtime, "_stopped", False)
    monkeypatch.setattr(mcp_runtime, "_lock", asyncio.Lock())
    monkeypatch.setattr(mcp_runtime, "_registry", ToolRegistry())
    monkeypatch.setattr(mcp_runtime, "_app_stacks", {})
    monkeypatch.setattr(mcp_runtime, "_app_tools", {})
    monkeypatch.setattr(mcp_runtime, "_app_servers", {})
    monkeypatch.setattr(mcp_runtime, "_app_configs", {})
    monkeypatch.setattr(mcp_runtime, "_app_states", {})
    monkeypatch.setattr(mcp_runtime, "_reconnect_at", {})
    monkeypatch.setattr(mcp_runtime, "_connecting", {})
    monkeypatch.setattr(state, "_agent", None)
    monkeypatch.setattr(
        "tools.tool_surface.mcp_tool_visible_here",
        lambda tool: getattr(tool, "mcp_app_id", None) in apps,
    )

    async def load(fn, *args, **kwargs):
        if fn.__name__ == "load_runtime_app_config":
            return apps[args[0]]
        if fn.__name__ == "load_missing_app_key_refs":
            return []
        if fn.__name__ == "record_runtime_app_state":
            return None
        if fn.__name__ == "resolve":
            return {}, {"X-Api-Key": "scratch-key"}
        raise AssertionError(fn.__name__)

    monkeypatch.setattr(mcp_runtime, "run_in_db", load)
    monkeypatch.setattr(mcp_module, "run_in_db", load)

    key = {"accepted": False}

    async def open_session(url, options, auth, stack):
        if not key["accepted"]:
            request = httpx.Request("POST", url)
            raise httpx.HTTPStatusError(
                "unauthorized", request=request,
                response=httpx.Response(401, request=request),
            )
        return _FakeSession("search"), types.Implementation(name="Wiki", version="1"), None

    monkeypatch.setattr(mcp_http_transport, "open_streamable_http_session", open_session)
    return mcp_runtime, key


async def _tools_load(registry, server: str) -> str:
    """Call the real ``tools_load`` on one harness registry, as a thread does."""
    return await registry.get("tools_load").execute(server=server)


def test_thread_that_saw_a_401_loads_the_app_after_the_key_is_replaced(app_runtime, tmp_path):
    from subagents.harness_tool_bridge import build_tool_registry

    mcp_runtime, key = app_runtime
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    async def scenario():
        await mcp_runtime.apply_connected_app_runtime("connect", "app-1")

        # The live harness session starts here and holds this registry for every later turn.
        thread_registry = build_tool_registry(str(workspace))
        refused = await _tools_load(thread_registry, "Wiki")
        assert "HTTP 401: authentication rejected" in refused

        key["accepted"] = True
        working = await mcp_runtime.apply_connected_app_runtime("restart", "app-1")
        assert (working["state"], working["tool_count"]) == ("working", 1)

        loaded = await _tools_load(thread_registry, "Wiki")
        assert not loaded.startswith("Error:"), loaded
        assert "mcp_Wiki_search" in loaded
        assert await thread_registry.get("mcp_Wiki_search").execute() == "ok"
        await mcp_runtime.stop_mcp_runtime()

    asyncio.run(scenario())


def test_a_connected_attempt_discards_the_previous_failure_for_that_server(app_runtime):
    """The failure a reconnect cleared is gone process-wide, not only where it was set.

    The failed connect already dropped the app's server name, so withdrawing the
    app cannot be what clears it.
    """
    mcp_runtime, key = app_runtime

    async def scenario():
        await mcp_runtime.apply_connected_app_runtime("connect", "app-1")
        assert "Wiki" in mcp_runtime._registry.mcp_server_states
        assert mcp_runtime._app_servers == {}

        key["accepted"] = True
        await mcp_runtime.apply_connected_app_runtime("restart", "app-1")
        assert mcp_runtime._registry.mcp_server_states == {}
        await mcp_runtime.stop_mcp_runtime()

    asyncio.run(scenario())


class _NamedTool:
    """A built-in that holds a public name."""

    def __init__(self, name: str) -> None:
        self.name = name


def test_a_connect_reaches_a_registry_built_at_any_moment(app_runtime):
    """Nothing is copied, so no registry can sit between a copy and a link."""
    from tools.registry import ToolRegistry

    mcp_runtime, key = app_runtime
    before, key["accepted"] = ToolRegistry(), True
    mcp_runtime.register_mcp_tools(before)

    async def scenario():
        await mcp_runtime.apply_connected_app_runtime("connect", "app-1")
        after = ToolRegistry()
        mcp_runtime.register_mcp_tools(after)
        published = mcp_runtime._registry.get("mcp_Wiki_search")
        assert before.get("mcp_Wiki_search") is published
        assert after.get("mcp_Wiki_search") is published
        assert "mcp_Wiki_search" in before.tool_names

        await mcp_runtime.apply_connected_app_runtime("remove", "app-1")
        assert not before.has("mcp_Wiki_search")
        assert not after.has("mcp_Wiki_search")
        await mcp_runtime.stop_mcp_runtime()

    asyncio.run(scenario())


def test_a_stop_leaves_nothing_in_a_registry_that_outlives_it(app_runtime):
    from tools.registry import ToolRegistry

    mcp_runtime, key = app_runtime
    registry, key["accepted"] = ToolRegistry(), True
    mcp_runtime.register_mcp_tools(registry)

    async def scenario():
        await mcp_runtime.apply_connected_app_runtime("connect", "app-1")
        assert registry.has("mcp_Wiki_search")
        await mcp_runtime.stop_mcp_runtime()
        assert registry.tool_names == []

    asyncio.run(scenario())


def test_reading_a_registry_off_the_event_loop_survives_a_connect(app_runtime):
    """An artifact review reads its registry in a worker thread while an app connects."""
    from tools.registry import ToolRegistry

    mcp_runtime, _key = app_runtime
    registry = ToolRegistry()
    mcp_runtime.register_mcp_tools(registry)
    done = threading.Event()
    failures: list[BaseException] = []

    def read() -> None:
        try:
            while not done.is_set():
                registry.tool_names
        except BaseException as error:
            failures.append(error)

    reader = threading.Thread(target=read, daemon=True)
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-5)
    reader.start()
    try:
        for round_ in range(2000):
            names = [f"mcp_Wiki_{round_}_{index}" for index in range(50)]
            for name in names:
                mcp_runtime._registry.register(_NamedTool(name))
            for name in names:
                mcp_runtime._registry.unregister(name)
    finally:
        done.set()
        reader.join()
        sys.setswitchinterval(interval)
    assert failures == []


def test_a_built_in_keeps_its_name_and_the_app_still_reaches_the_registry(app_runtime, apps):
    from tools.registry import ToolRegistry

    mcp_runtime, key = app_runtime
    apps["app-2"] = ("Docs", _app_config("app-2"))
    colliding = ToolRegistry()
    builtin = _NamedTool("mcp_Wiki_search")
    colliding.register(builtin)
    mcp_runtime.register_mcp_tools(colliding)
    key["accepted"] = True

    async def scenario():
        working = await mcp_runtime.apply_connected_app_runtime("connect", "app-1")
        assert (working["state"], working["tool_count"]) == ("working", 1)
        await mcp_runtime.apply_connected_app_runtime("connect", "app-2")
        assert colliding.get("mcp_Wiki_search") is builtin
        assert colliding.tool_names.count("mcp_Wiki_search") == 1
        assert colliding.get("mcp_Docs_search") is mcp_runtime._registry.get("mcp_Docs_search")
        await mcp_runtime.stop_mcp_runtime()

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["remove", "restart"])
def test_withdrawing_an_app_leaves_the_built_in_that_kept_its_name(app_runtime, action):
    from tools.registry import ToolRegistry

    mcp_runtime, key = app_runtime
    colliding, clear = ToolRegistry(), ToolRegistry()
    builtin = _NamedTool("mcp_Wiki_search")
    colliding.register(builtin)
    mcp_runtime.register_mcp_tools(colliding)
    mcp_runtime.register_mcp_tools(clear)
    key["accepted"] = True

    async def scenario():
        await mcp_runtime.apply_connected_app_runtime("connect", "app-1")
        assert clear.has("mcp_Wiki_search")

        await mcp_runtime.apply_connected_app_runtime(action, "app-1")
        assert colliding.get("mcp_Wiki_search") is builtin
        assert clear.has("mcp_Wiki_search") is (action == "restart")
        await mcp_runtime.stop_mcp_runtime()

    asyncio.run(scenario())


def test_an_allowlisted_native_agent_stays_exact_across_turns_and_connects(app_runtime, monkeypatch, tmp_path):
    """Every native turn asks for connected-app tools again, after the allowlist was applied."""
    from core.agent import CORSIAAgent, _apply_eval_tool_allowlist
    from infra import state

    mcp_runtime, key = app_runtime
    agent = CORSIAAgent(workspace=tmp_path)
    agent.tools.register(_NamedTool("bridge_exec"))
    mcp_runtime.register_mcp_tools(agent.tools)
    monkeypatch.setenv("CORSIA_TEST_MODE", "1")
    monkeypatch.setenv("CORSIA_EVAL_TOOL_ALLOWLIST", '["bridge_exec"]')
    _apply_eval_tool_allowlist(agent)
    monkeypatch.setattr(state, "_agent", agent)
    key["accepted"] = True

    async def scenario():
        await mcp_runtime.apply_connected_app_runtime("connect", "app-1")
        await agent._connect_mcp()
        assert agent.tools.tool_names == ["bridge_exec"]
        assert not agent.tools.has("mcp_Wiki_search")
        await mcp_runtime.apply_connected_app_runtime("restart", "app-1")
        await agent._connect_mcp()
        assert agent.tools.tool_names == ["bridge_exec"]
        await mcp_runtime.stop_mcp_runtime()

    asyncio.run(scenario())


def test_an_allowlist_can_name_a_tool_a_connected_app_published(app_runtime, monkeypatch):
    from core.agent import _apply_eval_tool_allowlist
    from tools.registry import ToolRegistry

    mcp_runtime, key = app_runtime
    agent = SimpleNamespace(tools=ToolRegistry())
    mcp_runtime.register_mcp_tools(agent.tools)
    key["accepted"] = True
    monkeypatch.setenv("CORSIA_TEST_MODE", "1")
    monkeypatch.setenv("CORSIA_EVAL_TOOL_ALLOWLIST", '["mcp_Wiki_search"]')

    async def scenario():
        await mcp_runtime.apply_connected_app_runtime("connect", "app-1")
        _apply_eval_tool_allowlist(agent)
        assert agent.tools.tool_names == ["mcp_Wiki_search"]
        await mcp_runtime.stop_mcp_runtime()

    asyncio.run(scenario())
