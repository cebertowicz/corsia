"""MCP reload serializes with start and stop, and an import's effect on live tool sets is fixed."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

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

    monkeypatch.setattr(mcp_runtime, "_stack", None, raising=False)
    monkeypatch.setattr(mcp_runtime, "_stopped", False)
    monkeypatch.setattr(mcp_runtime, "_lock", asyncio.Lock())
    monkeypatch.setattr(mcp_runtime, "_registry", ToolRegistry())
    monkeypatch.setattr(mcp_runtime, "_connected", set(), raising=False)
    monkeypatch.setattr(mcp_runtime, "_app_stacks", {})
    monkeypatch.setattr(mcp_runtime, "_app_tools", {})
    monkeypatch.setattr(mcp_runtime, "_app_servers", {})
    monkeypatch.setattr(mcp_runtime, "_app_configs", {})
    monkeypatch.setattr(mcp_runtime, "_app_states", {})
    monkeypatch.setattr(mcp_runtime, "_reconnect_at", {})
    monkeypatch.setattr(mcp_runtime, "_connecting", {})
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
    monkeypatch = pytest.MonkeyPatch()
    try:
        _runtime_db(monkeypatch, mcp_runtime, {"app-1": ("GitHub", {"connected_app_id": "app-1"})})

        async def scenario():
            gate["hold"], gate["entered"] = asyncio.Event(), asyncio.Event()
            connect = asyncio.create_task(mcp_runtime.apply_connected_app_runtime("connect", "app-1"))
            await gate["entered"].wait()
            stop = asyncio.create_task(mcp_runtime.stop_mcp_runtime())
            await asyncio.sleep(0)
            assert not stop.done()
            gate["hold"].set()
            await connect
            await stop

        asyncio.run(scenario())

        assert [tool.name for tool in tools] == ["GitHub_tool"]
        assert not any(tool.session_open for tool in tools)
        assert mcp_runtime._app_stacks == {}
    finally:
        monkeypatch.undo()


def test_reload_after_stop_opens_nothing(runtime):
    mcp_runtime, tools, _ = runtime
    monkeypatch = pytest.MonkeyPatch()
    try:
        _runtime_db(monkeypatch, mcp_runtime, {"app-1": ("GitHub", {"connected_app_id": "app-1"})})

        async def scenario():
            await mcp_runtime.stop_mcp_runtime()
            result = await mcp_runtime.apply_connected_app_runtime("reconnect", "app-1")
            assert result["failure_class"] == "cannot_start"

        asyncio.run(scenario())
        assert tools == []
        assert mcp_runtime._app_stacks == {}
    finally:
        monkeypatch.undo()


def test_start_after_stop_runs_again(runtime):
    mcp_runtime, tools, _ = runtime
    monkeypatch = pytest.MonkeyPatch()
    try:
        _runtime_db(monkeypatch, mcp_runtime, {"app-1": ("GitHub", {"connected_app_id": "app-1"})})

        async def scenario():
            await mcp_runtime.stop_mcp_runtime()
            results = await mcp_runtime.start_connected_app_runtime()
            assert results["app-1"]["state"] == "working"

        asyncio.run(scenario())
        assert [tool.name for tool in tools] == ["GitHub_tool"]
        assert tools[0].session_open
    finally:
        monkeypatch.undo()


def test_import_adds_tools_to_the_native_block_and_it_is_stable_afterwards(runtime, monkeypatch):
    from core.agent import CORSIAAgent
    from subagents.harness_tool_bridge import build_tool_registry

    mcp_runtime, tools, _ = runtime
    _runtime_db(monkeypatch, mcp_runtime, {
        "app-1": ("GitHub", {"connected_app_id": "app-1", "version": "one"}),
        "app-2": ("Slack", {"connected_app_id": "app-2", "version": "two"}),
    })

    async def scenario():
        for _ in range(2):
            results = await mcp_runtime.start_connected_app_runtime()
            assert {app_id: result["state"] for app_id, result in results.items()} == {
                "app-1": "working", "app-2": "working",
            }
            native = CORSIAAgent(workspace=Path.cwd())
            await native._connect_mcp()
            harness = build_tool_registry(str(Path.cwd()))
            assert native.tools.tool_names == ["GitHub_one", "Slack_two"]
            assert {name for name in harness.tool_names if name in native.tools.tool_names} == set(native.tools.tool_names)
            assert await native.tools.get("GitHub_one").execute() == "ok"
            assert await harness.get("Slack_two").execute() == "ok"
            await mcp_runtime.stop_mcp_runtime()

    asyncio.run(scenario())
    assert [tool.name for tool in tools] == ["GitHub_one", "Slack_two", "GitHub_one", "Slack_two"]
    assert not any(tool.session_open for tool in tools)


def _runtime_db(monkeypatch, mcp_runtime, apps):
    async def load(fn, *args):
        if fn.__name__ == "load_runtime_app_ids":
            return list(apps)
        if fn.__name__ == "load_runtime_app_config":
            return apps[args[0]]
        if fn.__name__ == "load_missing_app_key_refs":
            return []
        if fn.__name__ == "register_stored":
            return 0
        if fn.__name__ == "record_runtime_app_state":
            return None
        raise AssertionError(fn.__name__)

    monkeypatch.setattr(mcp_runtime, "run_in_db", load)


def test_connected_app_reconnect_rate_limit_names_the_ten_second_wait(runtime):
    from tools.mcp_runtime import MCPReconnectRateLimitError

    mcp_runtime, _tools, _gate = runtime
    mcp_runtime._reconnect_at["app-1"] = mcp_runtime.time.monotonic()

    with pytest.raises(MCPReconnectRateLimitError, match="try again in 10 seconds"):
        asyncio.run(mcp_runtime.apply_connected_app_runtime("reconnect", "app-1"))


def test_connect_restarts_a_running_app_when_its_stored_config_changed(runtime, monkeypatch):
    mcp_runtime, tools, _ = runtime
    versions = {"app-1": 1}

    async def load(fn, *args):
        if fn.__name__ == "load_runtime_app_config":
            app_id = args[0]
            return "GitHub", {"connected_app_id": app_id, "version": versions[app_id]}
        if fn.__name__ == "load_missing_app_key_refs":
            return []
        if fn.__name__ == "record_runtime_app_state":
            return None
        raise AssertionError(fn.__name__)

    monkeypatch.setattr(mcp_runtime, "run_in_db", load)

    async def scenario():
        first = await mcp_runtime.apply_connected_app_runtime("connect", "app-1")
        assert first["state"] == "working"
        versions["app-1"] = 2
        refreshed = await mcp_runtime.apply_connected_app_runtime("connect", "app-1")
        assert refreshed["state"] == "working"

    asyncio.run(scenario())

    assert [tool.name for tool in tools] == ["GitHub_1", "GitHub_2"]
    assert not tools[0].session_open
    assert tools[1].session_open


def test_a_saved_write_inside_the_reconnect_window_restarts_at_once(runtime, monkeypatch):
    """A person's saved change is not a Reconnect press: it reaches the app now, even seconds after one."""
    from tools.connected_apps_runtime import restart_after_saved_write

    mcp_runtime, tools, _ = runtime
    version = [1]

    async def load(fn, *args):
        if fn.__name__ == "load_runtime_app_config":
            return "GitHub", {"connected_app_id": args[0], "version": version[0]}
        if fn.__name__ == "load_missing_app_key_refs":
            return []
        if fn.__name__ == "record_runtime_app_state":
            return None
        raise AssertionError(fn.__name__)

    monkeypatch.setattr(mcp_runtime, "run_in_db", load)

    async def scenario():
        assert (await mcp_runtime.apply_connected_app_runtime("reconnect", "app-1"))["state"] == "working"
        version[0] = 2
        saved = await restart_after_saved_write("app-1")
        assert saved["state"] == "working"
        with pytest.raises(mcp_runtime.MCPReconnectRateLimitError):
            await mcp_runtime.apply_connected_app_runtime("reconnect", "app-1")

    asyncio.run(scenario())

    assert [tool.name for tool in tools] == ["GitHub_1", "GitHub_2"]
    assert not tools[0].session_open and tools[1].session_open


def test_connected_app_reconnect_and_remove_own_only_their_app_runtime(runtime, monkeypatch):
    from infra import state
    from tools.registry import ToolRegistry

    mcp_runtime, tools, _ = runtime
    versions = {"app-1": 1}

    async def load(fn, *args):
        if fn.__name__ == "load_runtime_app_config":
            app_id = args[0]
            return "GitHub", {"connected_app_id": app_id, "version": versions[app_id]}
        if fn.__name__ == "load_missing_app_key_refs":
            return []
        if fn.__name__ == "register_stored":
            return 0
        if fn.__name__ == "record_runtime_app_state":
            return None
        raise AssertionError(fn.__name__)

    monkeypatch.setattr(mcp_runtime, "run_in_db", load)

    async def scenario():
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


def test_remove_ends_a_stuck_connect_and_never_queues_behind_it(runtime, monkeypatch):
    mcp_runtime, tools, gate = runtime
    _runtime_db(monkeypatch, mcp_runtime, {
        "app-1": ("Figma", {"connected_app_id": "app-1"}),
        "app-2": ("Linear", {"connected_app_id": "app-2"}),
    })
    gate["hold"] = asyncio.Event()  # a server that never answers
    gate["entered"] = asyncio.Event()

    async def scenario():
        stuck = asyncio.create_task(mcp_runtime.apply_connected_app_runtime("reconnect", "app-1"))
        await asyncio.wait_for(gate["entered"].wait(), 2)
        # Another app's Remove does not wait for the stuck connect's lock.
        other = await asyncio.wait_for(mcp_runtime.apply_connected_app_runtime("remove", "app-2"), 2)
        assert other["state"] == "disconnected"
        # This app's own Remove ends the stuck connect at once and frees the lock.
        removed = await asyncio.wait_for(mcp_runtime.apply_connected_app_runtime("remove", "app-1"), 2)
        assert removed["state"] == "disconnected"
        assert (await asyncio.wait_for(stuck, 2))["state"] == "disconnected"
        assert mcp_runtime._connecting == {} and not mcp_runtime._lock.locked()
        gate["hold"] = None
        again = await asyncio.wait_for(mcp_runtime.apply_connected_app_runtime("connect", "app-2"), 2)
        assert again["state"] == "working"
        await mcp_runtime.stop_mcp_runtime()

    asyncio.run(scenario())
    assert not any(tool.session_open for tool in tools)


def test_cancelling_the_caller_ends_a_stuck_connect_and_frees_the_lock(runtime, monkeypatch):
    mcp_runtime, _tools, gate = runtime
    _runtime_db(monkeypatch, mcp_runtime, {"app-1": ("Figma", {"connected_app_id": "app-1"})})
    gate["hold"] = asyncio.Event()
    gate["entered"] = asyncio.Event()

    async def scenario():
        stuck = asyncio.create_task(mcp_runtime.apply_connected_app_runtime("connect", "app-1"))
        await asyncio.wait_for(gate["entered"].wait(), 2)
        stuck.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stuck
        assert mcp_runtime._connecting == {} and not mcp_runtime._lock.locked()

    asyncio.run(scenario())


def test_a_reconnect_queued_behind_another_apps_connect_is_ended_by_its_apps_remove(runtime, monkeypatch):
    mcp_runtime, tools, gate = runtime
    apps = {"app-a": ("Figma", {"connected_app_id": "app-a"}), "app-b": ("Linear", {"connected_app_id": "app-b"})}
    published = []

    async def load(fn, *args):
        if fn.__name__ == "load_runtime_app_config":
            return apps[args[0]]  # the row is gone once its app is removed
        if fn.__name__ == "load_missing_app_key_refs":
            return []
        if fn.__name__ == "record_runtime_app_state":
            published.append((args[0], args[1]["state"]))
            return None
        raise AssertionError(fn.__name__)

    monkeypatch.setattr(mcp_runtime, "run_in_db", load)
    gate["hold"], gate["entered"] = asyncio.Event(), asyncio.Event()  # app B's server never answers

    async def scenario():
        holding = asyncio.create_task(mcp_runtime.apply_connected_app_runtime("connect", "app-b"))
        await asyncio.wait_for(gate["entered"].wait(), 2)
        queued = asyncio.create_task(mcp_runtime.apply_connected_app_runtime("reconnect", "app-a"))
        await asyncio.sleep(0.05)  # Reconnect A now waits on the lock
        del apps["app-a"]
        removed = await asyncio.wait_for(mcp_runtime.apply_connected_app_runtime("remove", "app-a"), 2)
        assert removed["state"] == "disconnected"
        published.clear()
        gate["hold"].set()
        assert (await asyncio.wait_for(holding, 2))["state"] == "working"
        assert (await asyncio.wait_for(queued, 2))["state"] == "disconnected"
        await mcp_runtime.stop_mcp_runtime()

    asyncio.run(scenario())
    assert not any(app_id == "app-a" for app_id, _state in published)
    assert "app-a" not in mcp_runtime._app_states and not any(tool.name.startswith("Figma") for tool in tools)
    assert mcp_runtime._connecting == {}


def test_connected_app_with_an_unset_key_never_attempts_a_connection(runtime, monkeypatch):
    mcp_runtime, _tools, _gate = runtime
    published = []

    async def load(fn, *args):
        if fn.__name__ == "load_runtime_app_config":
            return "GitHub", {"connected_app_id": args[0], "secret_env": ["GITHUB_TOKEN"]}
        if fn.__name__ == "load_missing_app_key_refs":
            return ["GITHUB_TOKEN"]
        if fn.__name__ == "register_stored":
            return 0
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
        if fn.__name__ == "load_missing_app_key_refs":
            return []
        if fn.__name__ == "register_stored":
            return 0
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


def test_connect_runs_the_real_key_lookup_for_a_local_and_a_keyed_app(runtime, isolated_db):
    """Only the transport is faked; every DB job, the missing-key lookup included, really runs."""
    from connections.app_lifecycle import create_app
    from infra.database import get_db

    mcp_runtime, _tools, _gate = runtime
    everywhere = {"mode": "all", "project_ids": [], "outside_projects": False}
    db = get_db()
    try:
        local = create_app("Local", {"command": "fixture", "key_refs": []}, everywhere, db=db)
        keyed = create_app(
            "Keyed", {"url": "https://example.test/mcp", "transport": "http", "needs_token": True,
                      "key_refs": [{"kind": "header", "name": "Authorization"}]},
            everywhere, db=db,
        )
    finally:
        db.close()

    async def scenario():
        return (
            await mcp_runtime.apply_connected_app_runtime("connect", str(local["id"])),
            await mcp_runtime.apply_connected_app_runtime("connect", str(keyed["id"])),
        )

    local_result, keyed_result = asyncio.run(scenario())

    assert local_result["state"] == "working"
    assert keyed_result["state"] == "needs_token"
    assert mcp_runtime.connected_app_state(str(keyed["id"]))["state"] == "needs_token"


@pytest.mark.parametrize("live_state", ["needs_sign_in", "working", "connecting"])
def test_no_new_app_starts_in_a_live_state(runtime, isolated_db, live_state):
    """A-R16: a new app is a person's add or a request waiting for approval; startup connects neither kind early."""
    from connections.app_lifecycle import create_app
    from connections.app_reach import AppReachError
    from infra.database import get_db

    mcp_runtime, tools, _gate = runtime
    everywhere = {"mode": "all", "project_ids": [], "outside_projects": False}
    db = get_db()
    try:
        with pytest.raises(AppReachError):
            create_app("Public", {"url": "https://example.test/mcp", "key_refs": []}, everywhere, state=live_state, db=db)
        waiting = create_app("Public", {"url": "https://example.test/mcp", "key_refs": []}, everywhere, state="awaiting_approval", db=db)
    finally:
        db.close()

    assert str(waiting["id"]) not in asyncio.run(mcp_runtime.start_connected_app_runtime())
    assert tools == []


def test_runtime_error_after_connecting_publishes_failed_and_a_later_connect_retries(runtime, monkeypatch):
    from infra import state

    mcp_runtime, tools, _gate = runtime
    published = []

    async def load(fn, *args):
        if fn.__name__ == "load_runtime_app_config":
            return "GitHub", {"connected_app_id": args[0]}
        if fn.__name__ == "load_missing_app_key_refs":
            return []
        if fn.__name__ == "register_stored":
            return 0
        if fn.__name__ == "record_runtime_app_state":
            published.append(args[1])
            return None
        raise AssertionError(fn.__name__)

    class _Agent:
        def __init__(self) -> None:
            from tools.registry import ToolRegistry

            self.tools = ToolRegistry()

    calls = {"n": 0}

    def flaky_register(_tools) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("fixture registration failed")

    monkeypatch.setattr(mcp_runtime, "run_in_db", load)
    monkeypatch.setattr(mcp_runtime, "register_mcp_tools", flaky_register)
    monkeypatch.setattr(state, "_agent", _Agent())

    first = asyncio.run(mcp_runtime.apply_connected_app_runtime("connect", "app-1"))

    assert first["state"] == "failed" and first["failure_class"] == "cannot_start"
    assert mcp_runtime.connected_app_state("app-1")["state"] == "failed"
    assert [item["state"] for item in published][-1] == "failed"
    assert "app-1" not in mcp_runtime._app_stacks
    assert not any(tool.session_open for tool in tools)

    second = asyncio.run(mcp_runtime.apply_connected_app_runtime("connect", "app-1"))

    assert second["state"] == "working"
    assert mcp_runtime.connected_app_state("app-1")["state"] == "working"


def test_slow_first_runtime_download_happens_before_the_connect_deadline(runtime, monkeypatch):
    """An approved app stays connecting while its one-time pinned runtime arrives."""
    mcp_runtime, _tools, _gate = runtime
    _runtime_db(monkeypatch, mcp_runtime, {
        "app-1": ("Local", {"connected_app_id": "app-1", "command": "npx", "connect_timeout": 0.01}),
    })
    prepared = []

    def fetch_plan(command, *, path=None):
        assert (command, path) == ("npx", None)
        return "node", object()

    def slow_download(kind):
        prepared.append(kind)
        time.sleep(0.03)

    monkeypatch.setattr("tools.managed_runtime.runtime_fetch_plan", fetch_plan)
    monkeypatch.setattr("tools.managed_runtime.ensure_runtime", slow_download)

    result = asyncio.run(mcp_runtime.apply_connected_app_runtime("connect", "app-1"))

    assert result["state"] == "working"
    assert prepared == ["node"]


def test_runtime_download_failure_reconnects_without_cached_failure(runtime, monkeypatch):
    from tools.managed_runtime import ManagedRuntimeDownloadError

    mcp_runtime, _tools, _gate = runtime
    _runtime_db(monkeypatch, mcp_runtime, {
        "app-1": ("Local", {"connected_app_id": "app-1", "command": "npx"}),
    })
    attempts = 0

    def fetch_plan(_command, *, path=None):
        return "node", object()

    def fetch_runtime(_kind):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ManagedRuntimeDownloadError("download failed: URLError")

    monkeypatch.setattr("tools.managed_runtime.runtime_fetch_plan", fetch_plan)
    monkeypatch.setattr("tools.managed_runtime.ensure_runtime", fetch_runtime)

    failed = asyncio.run(mcp_runtime.apply_connected_app_runtime("connect", "app-1"))
    working = asyncio.run(mcp_runtime.apply_connected_app_runtime("reconnect", "app-1"))

    assert failed["failure_class"] == "runtime_download_failed"
    assert working["state"] == "working"
    assert attempts == 2
