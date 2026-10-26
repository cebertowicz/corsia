"""A connected-app result row records the app and the upstream tool that produced it."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tools.mcp_stamp import app_identity

_REMOTE = {
    "url": "https://mcp.example.com/v1/",
    "secret_headers": ["Authorization"],
}
_LOCAL = {"command": "npx", "args": ["-y", "@scope/server-github@1.2.3", "--token", "ghp_SECRETVALUE"]}


def _wrapper(server: str, tool: str, config: dict, *, app_id: str = "app-1"):
    from tools.mcp import MCPToolWrapper

    definition = SimpleNamespace(
        name=tool, description="upstream tool", inputSchema={"type": "object", "properties": {}},
    )
    return MCPToolWrapper(SimpleNamespace(), server, definition, config=config, app_id=app_id)


@pytest.fixture
def live(monkeypatch):
    """A fresh live-tool registry; each test registers the wrappers it needs."""
    from tools import mcp_runtime
    from tools.registry import ToolRegistry

    registry = ToolRegistry()
    monkeypatch.setattr(mcp_runtime, "_registry", registry)
    return registry


def _persist(name: str) -> dict:
    from core.tool_result_delivery import persist_tool_execution_result
    from infra.database import get_db
    from subagents.child_thread import persist_tool_use

    db = get_db()
    db.execute(
        "INSERT OR IGNORE INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','codex/gpt','t','t')",
    )
    db.commit()
    call_id = f"call-{name}"
    step_id = persist_tool_use(
        "thread-1", turn_id="turn-1", provider_turn_id="provider-turn-1",
        tool_id=call_id, name=name, arguments={"query": "private argument"}, db=db,
    )
    persisted = persist_tool_execution_result(
        thread_id="thread-1", turn_id="turn-1", invocation_id=f"inv-{name}",
        assistant_step_id=step_id, provider_tool_call_id=call_id, name=name,
        arguments={"query": "private argument"}, content="result text", is_error=False, db=db,
    )
    row = db.execute("SELECT metadata FROM messages WHERE id=?", (persisted.message_id,)).fetchone()
    db.close()
    return json.loads(row["metadata"])


def test_a_remote_and_a_local_call_persist_their_own_stamp(live, isolated_db):
    remote = _wrapper("alias_remote", "search_issues", _REMOTE)
    local = _wrapper("alias_local", "create_issue", _LOCAL, app_id="app-2")
    live.register(remote)
    live.register(local)

    remote_metadata = _persist(remote.name)
    local_metadata = _persist(local.name)

    assert (remote_metadata["mcp_app"], remote_metadata["mcp_tool"]) == (
        "https://mcp.example.com/v1", "search_issues",
    )
    assert (local_metadata["mcp_app"], local_metadata["mcp_tool"]) == (
        "@scope/server-github", "create_issue",
    )


def test_an_alias_rename_or_a_second_import_leaves_the_stamp_unchanged(live, isolated_db):
    first = _wrapper("github", "create_issue", _LOCAL, app_id="app-1")
    live.register(first)
    before = _persist(first.name)

    live.unregister(first.name)
    renamed = _wrapper("my_renamed_alias", "create_issue", _LOCAL, app_id="app-9")
    live.register(renamed)
    after = _persist(renamed.name)

    assert renamed.name != first.name
    assert {key: after[key] for key in ("mcp_app", "mcp_tool")} == {
        key: before[key] for key in ("mcp_app", "mcp_tool")
    }


def test_the_stamp_holds_no_secret_header_environment_or_argument(live, isolated_db):
    config = {
        **_REMOTE,
        "url": "https://user:urlpassword@mcp.example.com/v1?api_key=querysecret#frag",
        "headers": {"Authorization": "Bearer headersecret"},
        "env": {"API_TOKEN": "envsecret"},
    }
    remote = _wrapper("keyed", "search_issues", config)
    local = _wrapper("local", "create_issue", _LOCAL, app_id="app-2")
    live.register(remote)
    live.register(local)

    stored = json.dumps([_persist(remote.name), _persist(local.name)])

    for secret in ("urlpassword", "querysecret", "headersecret", "envsecret", "ghp_SECRETVALUE", "Bearer"):
        assert secret not in stored
    assert "private argument" in stored  # the call's own arguments are stored beside it, never in the stamp
    for metadata in (_persist(remote.name), _persist(local.name)):
        assert "private argument" not in json.dumps({k: metadata[k] for k in ("mcp_app", "mcp_tool")})


def test_a_native_tool_result_has_no_stamp(live, isolated_db):
    metadata = _persist("write_file")

    assert "mcp_app" not in metadata and "mcp_tool" not in metadata


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"url": "https://Mcp.Example.com:8443/path/?k=v"}, "https://mcp.example.com:8443/path"),
        ({"command": "npx", "args": ["-y", "pkg@latest"]}, "pkg"),
        ({"command": "uvx", "args": ["mcp-server-fetch==1.0"]}, "mcp-server-fetch"),
        ({"command": "uvx", "args": ["--from", "pkg-a", "run-it"]}, "pkg-a"),
        ({"command": "pnpm", "args": ["dlx", "@scope/pkg"]}, "@scope/pkg"),
        ({"command": "/usr/local/bin/my-server", "args": ["--token", "secret"]}, "my-server"),
        ({"command": "node", "args": ["/home/me/servers/notes.js"]}, "notes.js"),
        ({"command": "npx", "args": ["https://user:pw@host.example/pkg.tgz"]}, "https://host.example/pkg.tgz"),
        ({}, None),
    ],
)
def test_app_identity_names_the_server_not_its_launch_details(config, expected):
    assert app_identity(config) == expected
