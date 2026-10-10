"""MCP import never exposes client configuration values."""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager

import pytest

from tools import mcp_import


def _json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_detects_documented_client_formats_without_secret_values(tmp_path):
    _json(tmp_path / "Library/Application Support/Claude/claude_desktop_config.json", {
        "mcpServers": {"desktop": {"command": "npx", "args": ["--token", "desktop-secret"], "env": {"TOKEN": "desktop-secret"}}},
    })
    _json(tmp_path / ".claude.json", {
        "mcpServers": {
            "code": {"type": "http", "url": "https://example.test/mcp?token=code-secret", "headers": {"Authorization": "code-secret"}},
            "broken": {"url": "https://example.test/mcp"},
        },
    })
    _json(tmp_path / ".cursor/mcp.json", {
        "mcpServers": {"cursor": {"command": "uvx", "args": ["cursor-secret"]}},
    })
    codex = tmp_path / ".codex/config.toml"
    codex.parent.mkdir(parents=True)
    codex.write_text('[mcp_servers.codex]\nurl = "https://example.test/mcp?token=codex-secret"\n', encoding="utf-8")

    detected = mcp_import.detected_sources(tmp_path)

    assert detected == [
        {"source": "claude_desktop", "app": "Claude Desktop", "servers": [{"name": "desktop", "kind": "stdio"}]},
        {"source": "claude_code", "app": "Claude Code", "servers": [{"name": "code", "kind": "remote"}]},
        {"source": "cursor", "app": "Cursor", "servers": [{"name": "cursor", "kind": "stdio"}]},
        {"source": "codex", "app": "Codex", "servers": [{"name": "codex", "kind": "remote"}]},
    ]
    rendered = json.dumps(detected)
    assert "secret" not in rendered and "example.test" not in rendered and "args" not in rendered


def test_prepare_import_keeps_secret_out_of_status_and_rejects_literal_auth(tmp_path):
    _json(tmp_path / ".cursor/mcp.json", {
        "mcpServers": {
            "good": {"command": "npx", "args": ["-y", "server"], "env": {"TOKEN": "stdio-secret"}},
            "bad": {"url": "https://example.test/mcp", "headers": {"Authorization": "http-secret"}},
        },
    })

    additions, statuses = mcp_import.prepare_import(
        [{"source": "cursor", "name": "good"}, {"source": "cursor", "name": "bad"}], tmp_path,
    )

    assert additions["good"]["env"] == {"TOKEN": "stdio-secret"}
    assert statuses[0] == {"name": "good", "kind": "stdio", "status": "pending"}
    assert statuses[1]["name"] == "bad"
    assert statuses[1]["status"].startswith("failed:")
    assert "http-secret" not in json.dumps(statuses)


def test_prepare_import_refuses_duplicate_server_names_from_multiple_clients(tmp_path):
    _json(tmp_path / ".claude.json", {"mcpServers": {"same": {"command": "npx"}}})
    _json(tmp_path / ".cursor/mcp.json", {"mcpServers": {"same": {"command": "uvx"}}})

    additions, statuses = mcp_import.prepare_import([
        {"source": "claude_code", "name": "same"}, {"source": "cursor", "name": "same"},
    ], tmp_path)

    assert additions == {}
    assert statuses == [
        {"name": "same", "status": "failed: selected server name conflicts with another selection"},
        {"name": "same", "status": "failed: selected server name conflicts with another selection"},
    ]


class _FakeTool:
    def __init__(self, name):
        self.name = name
        self.session_open = True


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
    monkeypatch.setattr(state, "_agent", None)
    attempts = []

    async def fake_connect(config, registry, stack):
        errors = {}
        for name, server in config.items():
            attempts.append(name)
            if server.get("fail"):
                errors[name] = "unavailable"
                continue
            tool = _FakeTool(f"{name}_tool")
            registry.register(tool)

            async def close(tool=tool):
                tool.session_open = False

            stack.push_async_callback(close)
        return errors

    monkeypatch.setattr(mcp_runtime, "connect_mcp_servers", fake_connect)
    return mcp_runtime, attempts


def test_reload_adds_servers_without_disturbing_existing_sessions(runtime, monkeypatch):
    from infra import state
    from tools.registry import ToolRegistry

    mcp_runtime, attempts = runtime

    async def scenario():
        await mcp_runtime.start_mcp_runtime({"old": {}})
        cached = type("Agent", (), {"tools": ToolRegistry()})()
        mcp_runtime.register_mcp_tools(cached.tools)
        monkeypatch.setattr(state, "_agent", cached)
        old_tool = cached.tools.get("old_tool")

        errors = await mcp_runtime.reload_mcp_runtime(
            {"old": {}, "new": {}, "offline": {"fail": True}})
        try:
            assert errors == {"offline": "unavailable"}
            assert cached.tools.get("new_tool") is not None
            assert cached.tools.get("old_tool") is old_tool and old_tool.session_open
            assert attempts == ["old", "new", "offline"]
            await mcp_runtime.reload_mcp_runtime({"old": {}, "new": {}, "offline": {}})
            assert attempts == ["old", "new", "offline", "offline"]
        finally:
            await mcp_runtime.stop_mcp_runtime()
        assert not old_tool.session_open

    asyncio.run(scenario())


def test_reload_starts_the_runtime_when_it_never_started(runtime):
    mcp_runtime, _ = runtime

    async def scenario():
        try:
            assert await mcp_runtime.reload_mcp_runtime({"first": {}}) == {}
            assert mcp_runtime._registry.has("first_tool")
        finally:
            await mcp_runtime.stop_mcp_runtime()

    asyncio.run(scenario())


def test_save_import_classifies_matching_and_conflicting_existing_servers(monkeypatch):
    from infra import database, settings

    stored = {"tools": {"mcpServers": {"server": {"command": "existing"}}}}

    @contextmanager
    def fake_transaction(*_args, **_kwargs):
        yield object()

    monkeypatch.setattr(database, "write_transaction", fake_transaction)
    monkeypatch.setattr(settings, "get_setting", lambda key, db: stored.get(key))
    monkeypatch.setattr(settings, "set_setting", lambda key, value, db: stored.__setitem__(key, value))

    equal = mcp_import.save_import({"server": {"command": "existing"}})
    conflict = mcp_import.save_import({"server": {"command": "different"}})

    assert equal == {"server": "existing_equal"}
    assert conflict == {"server": "conflict"}
    assert stored["tools"]["mcpServers"] == {"server": {"command": "existing"}}


def test_import_reports_each_server_status_without_config(monkeypatch):
    from api import mcp
    from tools import mcp_import as importer

    async def fake_off_db(fn, *_args):
        assert fn is importer.prepare_import
        return ({"working": {"command": "fixture"}, "offline": {"command": "fixture"}}, [
            {"name": "working", "kind": "stdio", "status": "pending"},
            {"name": "offline", "kind": "stdio", "status": "pending"},
        ])

    async def fake_in_db(fn, *_args):
        if fn is importer.save_import:
            return {"working": "saved", "offline": "saved"}
        return {"working": {"command": "fixture"}, "offline": {"command": "fixture"}}

    async def fake_reload(config):
        assert set(config) == {"working", "offline"}
        return {"offline": "server unavailable"}

    monkeypatch.setattr(mcp, "run_off_db", fake_off_db)
    monkeypatch.setattr(mcp, "run_in_db", fake_in_db)
    monkeypatch.setattr("tools.mcp_runtime.reload_mcp_runtime", fake_reload)

    result = asyncio.run(mcp.import_servers({"servers": [
        {"source": "cursor", "name": "working"}, {"source": "cursor", "name": "offline"},
    ]}, {}))

    assert result == {"servers": [
        {"name": "working", "kind": "stdio", "status": "working"},
        {"name": "offline", "kind": "stdio", "status": "failed: server unavailable"},
    ]}


def test_import_reloads_a_matching_existing_server(monkeypatch):
    from api import mcp
    from tools import mcp_import as importer

    async def fake_off_db(_fn, *_args):
        return ({"existing": {"command": "fixture"}}, [
            {"name": "existing", "kind": "stdio", "status": "pending"},
        ])

    async def fake_in_db(fn, *_args):
        if fn is importer.save_import:
            return {"existing": "existing_equal"}
        return {"existing": {"command": "fixture"}}

    reloaded = []

    async def fake_reload(config):
        reloaded.append(config)
        return {}

    monkeypatch.setattr(mcp, "run_off_db", fake_off_db)
    monkeypatch.setattr(mcp, "run_in_db", fake_in_db)
    monkeypatch.setattr("tools.mcp_runtime.reload_mcp_runtime", fake_reload)

    result = asyncio.run(mcp.import_servers({"servers": [{"source": "cursor", "name": "existing"}]}, {}))

    assert result == {"servers": [{"name": "existing", "kind": "stdio", "status": "working"}]}
    assert reloaded == [{"existing": {"command": "fixture"}}]


def test_import_reports_conflicting_existing_server_without_reloading(monkeypatch):
    from api import mcp
    from tools import mcp_import as importer

    async def fake_off_db(_fn, *_args):
        return ({"existing": {"command": "fixture"}}, [
            {"name": "existing", "kind": "stdio", "status": "pending"},
        ])

    async def fake_in_db(fn, *_args):
        if fn is importer.save_import:
            return {"existing": "conflict"}
        pytest.fail("a conflicting import must not read settings for reload")

    async def fake_reload(_config):
        pytest.fail("a conflicting import must not reload MCP sessions")

    monkeypatch.setattr(mcp, "run_off_db", fake_off_db)
    monkeypatch.setattr(mcp, "run_in_db", fake_in_db)
    monkeypatch.setattr("tools.mcp_runtime.reload_mcp_runtime", fake_reload)

    result = asyncio.run(mcp.import_servers({"servers": [{"source": "cursor", "name": "existing"}]}, {}))

    assert result == {"servers": [{
        "name": "existing",
        "kind": "stdio",
        "status": "failed: a server named existing is already set up in CORSIA; kept the existing one",
    }]}


def test_empty_import_is_rejected():
    from fastapi import HTTPException
    from api import mcp

    with pytest.raises(HTTPException, match="nonempty source/name pairs") as error:
        asyncio.run(mcp.import_servers({"servers": []}, {}))
    assert error.value.status_code == 422


def test_conductor_routes_are_open():
    from types import SimpleNamespace
    from tools.conductor_api_policy import route_class

    for method, path in (("GET", "/api/mcp/sources"), ("POST", "/api/mcp/import")):
        assert route_class(SimpleNamespace(path=path, methods={method})) == "open"


def test_sources_route_returns_only_the_safe_detection_view(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.mcp import router
    from infra.auth import require_user_header_only

    monkeypatch.setattr(mcp_import, "detected_sources", lambda: [
        {"source": "cursor", "app": "Cursor", "servers": [{"name": "safe", "kind": "stdio"}]},
    ])
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user_header_only] = lambda: {}

    response = TestClient(app).get("/api/mcp/sources")

    assert response.json() == {"sources": [
        {"source": "cursor", "app": "Cursor", "servers": [{"name": "safe", "kind": "stdio"}]},
    ]}
