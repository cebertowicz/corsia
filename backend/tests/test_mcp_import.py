"""MCP discovery never exposes client configuration values or bypasses Connected Apps."""
from __future__ import annotations

import asyncio
import json

import pytest

from tools import mcp_import


def _json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_detects_documented_client_formats_without_secret_values(tmp_path):
    _json(tmp_path / "Library/Application Support/Claude/claude_desktop_config.json", {
        "mcpServers": {"desktop": {"command": "npx", "args": ["--token", "desktop-secret"], "env": {"TOKEN": "masked"}}},
    })
    _json(tmp_path / ".claude.json", {
        "mcpServers": {
            "code": {"type": "http", "url": "https://example.test/mcp", "headers": {"Authorization": "code-secret"}},
            "broken": {"url": "https://example.test/mcp"},
        },
    })
    _json(tmp_path / ".cursor/mcp.json", {
        "mcpServers": {"cursor": {"command": "uvx", "args": ["cursor-secret"]}},
    })
    codex = tmp_path / ".codex/config.toml"
    codex.parent.mkdir(parents=True)
    codex.write_text('[mcp_servers.codex]\nurl = "https://example.test/mcp"', encoding="utf-8")

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
            "good": {"command": "npx", "args": ["-y", "server"], "env": {"TOKEN": "masked"}},
            "bad": {"url": "https://example.test/mcp", "headers": {"Authorization": "http-secret"}},
        },
    })

    additions, statuses = mcp_import.prepare_import(
        [{"source": "cursor", "name": "good"}, {"source": "cursor", "name": "bad"}], tmp_path,
    )

    assert additions["good"]["env"] == {"TOKEN": "masked"}
    assert statuses[0] == {"name": "good", "kind": "stdio", "status": "pending"}
    assert additions["bad"]["headers"] == {"Authorization": "http-secret"}
    assert statuses[1] == {"name": "bad", "kind": "remote", "status": "pending"}
    assert "http-secret" not in json.dumps(statuses)


def test_prepare_import_refuses_duplicate_server_names_from_multiple_clients(tmp_path):
    _json(tmp_path / ".claude.json", {"mcpServers": {"same": {"command": "npx"}}})
    _json(tmp_path / ".cursor/mcp.json", {"mcpServers": {"same": {"command": "uvx"}}})

    additions, statuses = mcp_import.prepare_import([
        {"source": "claude_code", "name": "same"}, {"source": "cursor", "name": "same"},
    ], tmp_path)

    assert additions == {}
    assert statuses == [
        {"name": "same", "status": "failed: selected app name conflicts with another selection"},
        {"name": "same", "status": "failed: selected app name conflicts with another selection"},
    ]


def test_legacy_import_writer_is_refused_without_writing_settings():
    from tools.mcp_config import MCPConfigError

    with pytest.raises(MCPConfigError, match="Connected apps"):
        mcp_import.save_import({"server": {"command": "existing"}})


def test_import_route_rejects_override_fields():
    from fastapi import HTTPException
    from api import mcp

    with pytest.raises(HTTPException) as error:
        asyncio.run(mcp.import_servers({"servers": [{"source": "cursor", "name": "server", "url": "https://bad.test"}]}, {}))
    assert error.value.status_code == 422
    assert error.value.detail == "each selected app must contain only source and name"


def test_import_route_returns_awaiting_approval_for_a_valid_selection(tmp_path, isolated_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.mcp import router
    from infra.auth import require_user_header_only

    _json(tmp_path / ".cursor/mcp.json", {"mcpServers": {"fixture": {"command": "npx"}}})
    monkeypatch.setattr(mcp_import, "_home", lambda: tmp_path)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user_header_only] = lambda: {"id": "importer"}

    response = TestClient(app).post("/api/mcp/import", json={
        "servers": [{"source": "cursor", "name": "fixture"}],
    })

    assert response.status_code == 200
    assert response.json()["rows"] == [{
        "id": response.json()["rows"][0]["id"], "name": "fixture", "state": "awaiting_approval",
    }]


def test_proxy_authorization_import_is_classified_as_cannot_connect(tmp_path):
    import sqlite3

    from infra.database import initialize_schema

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    _json(tmp_path / ".claude.json", {"mcpServers": {"Proxy": {
        "type": "http", "url": "https://example.test/mcp", "headers": {"Proxy-Authorization": "fake"},
    }}})

    result = mcp_import.import_selected(
        [{"source": "claude_code", "name": "Proxy"}], db=db, home=tmp_path, actor="agent",
    )

    assert result == {"rows": [{"name": "Proxy", "status": "cannot_connect"}]}
    assert mcp_import.IMPORT_UNSUPPORTED == "CORSIA can't connect to that kind of app yet."


def test_import_rolls_back_pending_app_when_key_store_write_fails(tmp_path, monkeypatch):
    import sqlite3

    from infra import key_store
    from infra.database import initialize_schema

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    _json(tmp_path / ".claude.json", {"mcpServers": {
        "fails": {"type": "http", "url": "https://first.test/mcp", "headers": {"Authorization": "one"}},
        "works": {"type": "http", "url": "https://second.test/mcp", "headers": {"Authorization": "two"}},
    }})
    original = key_store.set_managed_value
    calls = 0

    def fail_once(name, value, *, db=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise key_store.KeyStoreError("injected failure")
        original(name, value, db=db)

    monkeypatch.setattr(key_store, "set_managed_value", fail_once)
    result = mcp_import.import_selected(
        [{"source": "claude_code", "name": "fails"}, {"source": "claude_code", "name": "works"}],
        db=db, home=tmp_path, actor="agent",
    )

    assert result["rows"] == [
        {"name": "fails", "status": "failed"},
        {"id": result["rows"][1]["id"], "name": "works", "state": "awaiting_approval"},
    ]
    assert db.execute("SELECT name FROM connected_apps").fetchall()[0]["name"] == "works"


def test_import_selected_creates_pending_app_and_keeps_key_out_of_row(tmp_path):
    import sqlite3

    from connections.app_reach import get_app
    from infra.database import initialize_schema

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    project = tmp_path / "corsia"
    project.mkdir()
    db.execute(
        "INSERT INTO projects(id,name,path,created_at,updated_at) VALUES (?,?,?,?,?)",
        ("project", "Corsia", str(project), "t", "t"),
    )
    db.commit()
    _json(tmp_path / ".claude.json", {
        "projects": {str(project): {"mcpServers": {
            "GitHub": {"type": "http", "url": "https://example.test/mcp", "headers": {"Authorization": "import-secret"}},
        }}},
    })

    result = mcp_import.import_selected([{"source": "claude_code", "name": "GitHub"}], db=db, home=tmp_path, actor="agent")

    assert result["rows"][0]["state"] == "awaiting_approval"
    app = get_app(result["rows"][0]["id"], db=db)
    rendered = json.dumps(app)
    assert "import-secret" not in rendered
    assert app["config"]["key_refs"] == [{"kind": "header", "name": "Authorization"}]
    assert app["reach"] == {"mode": "selected", "project_ids": ["project"], "outside_projects": False}
    assert db.execute("SELECT value FROM settings WHERE key='tools'").fetchone() is None


def test_codex_environment_tokens_stay_unread_and_need_a_token(tmp_path, monkeypatch, caplog):
    import sqlite3

    from connections.app_reach import get_app
    from infra import state
    from infra.database import initialize_schema
    from tools import mcp_runtime
    from tools.registry import ToolRegistry

    secret = "backend-environment-secret-for-codex-fixture"
    monkeypatch.setenv("X", secret)
    _json(tmp_path / ".codex/config.toml", {})
    (tmp_path / ".codex/config.toml").write_text(
        '[mcp_servers.codex]\nurl = "https://example.test/mcp"\nbearer_token_env_var = "X"\n'
        '[mcp_servers.codex.env_http_headers]\nAuthorization = "X"\n',
        encoding="utf-8",
    )
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)

    imported = mcp_import.import_selected(
        [{"source": "codex", "name": "codex"}], db=db, home=tmp_path, actor="agent",
    )
    app = get_app(imported["rows"][0]["id"], db=db)

    assert imported["rows"][0]["state"] == "awaiting_approval"
    assert app["config"]["needs_token"] is True
    assert app["config"]["key_refs"] == [{"kind": "header", "name": "Authorization"}]
    assert secret not in json.dumps({"row": app, "response": imported})
    assert secret not in str(db.execute("SELECT name,value_ref FROM key_store_entries").fetchall())

    monkeypatch.setattr(mcp_runtime, "_stopped", False)
    monkeypatch.setattr(mcp_runtime, "_lock", asyncio.Lock())
    monkeypatch.setattr(mcp_runtime, "_registry", ToolRegistry())
    monkeypatch.setattr(mcp_runtime, "_app_stacks", {})
    monkeypatch.setattr(mcp_runtime, "_app_tools", {})
    monkeypatch.setattr(mcp_runtime, "_app_servers", {})
    monkeypatch.setattr(mcp_runtime, "_app_states", {})
    monkeypatch.setattr(mcp_runtime, "_reconnect_at", {})
    monkeypatch.setattr(state, "_agent", None)
    published = []
    key_is_set = False

    async def load(fn, *args):
        if fn.__name__ == "load_runtime_app_config":
            return "codex", {
                **app["config"], "secret_headers": ["Authorization"], "connected_app_id": args[0],
            }
        if fn.__name__ == "missing_app_key_refs":
            return [] if key_is_set else ["Authorization"]
        if fn.__name__ == "record_runtime_app_state":
            published.append(args[1])
            return None
        raise AssertionError(fn.__name__)

    async def should_not_connect(*_args):
        raise AssertionError("an imported environment token must not open a connection")

    monkeypatch.setattr(mcp_runtime, "run_in_db", load)
    monkeypatch.setattr(mcp_runtime, "connect_mcp_servers", should_not_connect)
    result = asyncio.run(mcp_runtime.apply_connected_app_runtime("connect", str(app["id"])))

    assert result["state"] == "needs_token"
    assert [item["state"] for item in published] == ["connecting", "needs_token"]

    key_is_set = True

    async def connects(config, _registry, _stack):
        assert config["codex"]["secret_headers"] == ["Authorization"]
        return {}

    monkeypatch.setattr(mcp_runtime, "connect_mcp_servers", connects)
    result = asyncio.run(mcp_runtime.apply_connected_app_runtime("connect", str(app["id"])))

    assert result["state"] == "working"
    assert secret not in json.dumps(result)
    assert secret not in caplog.text


def test_project_reach_uses_real_paths_components_and_deepest_match(tmp_path):
    import sqlite3

    from infra.database import initialize_schema

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    outer = tmp_path / "a" / "b"
    nested = outer / "nested"
    sibling = tmp_path / "a" / "bc"
    nested.mkdir(parents=True)
    sibling.mkdir()
    for project_id, path in (("outer", outer), ("nested", nested)):
        db.execute("INSERT INTO projects(id,name,path,created_at,updated_at) VALUES (?,?,?,?,?)", (project_id, project_id, str(path), "t", "t"))
    db.commit()
    alias = tmp_path / "alias"
    alias.symlink_to(nested, target_is_directory=True)

    reach, missing = mcp_import._project_reach(alias / "work", db=db)
    sibling_reach, sibling_missing = mcp_import._project_reach(sibling, db=db)

    assert reach == {"mode": "selected", "project_ids": ["nested"], "outside_projects": False}
    assert missing is None
    assert sibling_reach is None
    assert sibling_missing == "bc"


def test_key_descriptor_rejects_non_env_or_header_kind_and_empty_headers_do_not_redact():
    import sqlite3

    from connections.app_reach import AppReachError, key_ref_descriptors
    from infra.database import initialize_schema
    from tools.mcp import _StoredHeadersAuth

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)

    with pytest.raises(AppReachError):
        key_ref_descriptors("app", {"key_refs": [{"kind": "runtime_app_config", "name": "bad"}]}, db=db)
    assert _StoredHeadersAuth({"X-Empty": ""}, "https://example.test").redact("safe") == "safe"


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
