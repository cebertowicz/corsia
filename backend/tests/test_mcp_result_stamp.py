"""A connected-app result row records the app and the upstream tool that produced it."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tools.mcp_stamp import app_identity, result_stamp

_REMOTE = {"url": "https://mcp.example.com/v1/", "secret_headers": ["Authorization"]}
_LOCAL = {"command": "npx", "args": ["-y", "@scope/server-github@1.2.3", "--token", "ghp_SECRETVALUE"]}


def _wrapper(server: str, tool: str, config: dict, *, app_id: str = "app-1", output: str = "result text"):
    from tools.mcp import MCPToolWrapper

    definition = SimpleNamespace(
        name=tool, description="upstream tool", inputSchema={"type": "object", "properties": {}},
    )
    session = SimpleNamespace(
        call_tool=AsyncMock(return_value=SimpleNamespace(content=[output], isError=False)),
    )
    return MCPToolWrapper(session, server, definition, config=config, app_id=app_id)


def _run(wrapper) -> str:
    """The wrapper's result as the registry hands it on, after its own sanitizing."""
    from tools.registry import ToolRegistry

    return ToolRegistry._sanitize_result(asyncio.run(wrapper.execute()))


def _persist(name: str, content: str = "result text") -> dict:
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
        arguments={"query": "private argument"}, content=content, is_error=False, db=db,
    )
    row = db.execute("SELECT metadata FROM messages WHERE id=?", (persisted.message_id,)).fetchone()
    db.close()
    return json.loads(row["metadata"])


def _stamp_of(metadata: dict) -> dict:
    return {key: metadata[key] for key in ("mcp_app", "mcp_tool") if key in metadata}


def test_a_remote_and_a_local_call_persist_their_own_stamp(isolated_db):
    remote = _wrapper("alias_remote", "search_issues", _REMOTE)
    local = _wrapper("alias_local", "create_issue", _LOCAL, app_id="app-2")

    assert _stamp_of(_persist(remote.name, _run(remote))) == {
        "mcp_app": "https://mcp.example.com/v1", "mcp_tool": "search_issues",
    }
    assert _stamp_of(_persist(local.name, _run(local))) == {
        "mcp_app": "@scope/server-github", "mcp_tool": "create_issue",
    }


def test_an_error_result_from_the_app_carries_the_stamp_too(isolated_db):
    wrapper = _wrapper("alias", "search_issues", _REMOTE)
    wrapper._session.call_tool.side_effect = RuntimeError("upstream failed")

    assert _stamp_of(_persist(wrapper.name, _run(wrapper))) == {
        "mcp_app": "https://mcp.example.com/v1", "mcp_tool": "search_issues",
    }


def test_an_alias_rename_or_a_second_import_leaves_the_stamp_unchanged(isolated_db):
    first = _wrapper("github", "create_issue", _LOCAL, app_id="app-1")
    renamed = _wrapper("my_renamed_alias", "create_issue", _LOCAL, app_id="app-9")

    assert renamed.name != first.name
    assert _stamp_of(_persist(first.name, _run(first))) == _stamp_of(_persist(renamed.name, _run(renamed)))


def test_a_restart_or_removal_between_the_call_and_the_write_cannot_change_the_stamp(isolated_db):
    """The stamp rides on the result of the wrapper that ran; no registry is read at write time."""
    from tools import mcp_runtime
    from tools.registry import ToolRegistry

    old = _wrapper("shared_alias", "search", _REMOTE)
    result = _run(old)
    # A restart under the same alias swaps in a different server; then the app is removed.
    new = _wrapper("shared_alias", "search", {"url": "https://other.example.com/mcp"}, app_id="app-2")
    registry = ToolRegistry()
    registry.register(new)
    mcp_runtime._registry = registry
    try:
        swapped = _persist(old.name, result)
        registry.unregister(new.name)
        removed = _persist(old.name + "-again", result)
    finally:
        mcp_runtime._registry = ToolRegistry()

    expected = {"mcp_app": "https://mcp.example.com/v1", "mcp_tool": "search"}
    assert _stamp_of(swapped) == expected
    assert _stamp_of(removed) == expected


def test_the_stamp_holds_no_secret_header_environment_or_argument(isolated_db):
    config = {
        **_REMOTE,
        "url": "https://user:urlpassword@mcp.example.com/v1?api_key=querysecret#frag",
        "headers": {"Authorization": "Bearer headersecret"},
        "env": {"API_TOKEN": "envsecret"},
    }
    remote = _wrapper("keyed", "search_issues", config)
    local = _wrapper("local", "create_issue", _LOCAL, app_id="app-2")
    flagged = _wrapper(
        "flagged", "read", {"command": "npx", "args": ["--api-key", "sk-live-abc", "pkg"]}, app_id="app-3",
    )
    stamps = [_stamp_of(_persist(w.name, _run(w))) for w in (remote, local, flagged)]

    assert stamps[2] == {"mcp_app": "pkg", "mcp_tool": "read"}
    assert "sk-live-abc" not in json.dumps(result_stamp(_run(flagged)))
    stored = json.dumps(stamps)
    for secret in ("urlpassword", "querysecret", "headersecret", "envsecret", "ghp_SECRETVALUE", "Bearer",
                   "private argument", "sk-live-abc"):
        assert secret not in stored


def test_a_native_tool_result_has_no_stamp(isolated_db):
    assert _stamp_of(_persist("write_file")) == {}
    assert result_stamp("plain text") == {}


def test_a_stamp_survives_the_registry_boundary_unchanged():
    wrapper = _wrapper("alias", "search_issues", _REMOTE)

    assert result_stamp(_run(wrapper)) == {"mcp_app": "https://mcp.example.com/v1", "mcp_tool": "search_issues"}


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"url": "https://Mcp.Example.com:8443/path/?k=v"}, "https://mcp.example.com:8443/path"),
        ({"command": "npx", "args": ["-y", "pkg@latest"]}, "pkg"),
        ({"command": "npx", "args": ["--api-key", "sk-live-abc", "pkg"]}, "pkg"),
        ({"command": "npx", "args": ["--api-key=sk-live-abc", "pkg"]}, "pkg"),
        ({"command": "npx", "args": ["--api-key", "sk-live-abc"]}, "npx"),
        ({"command": "uvx", "args": ["--python", "3.12", "pkg"]}, "pkg"),
        ({"command": "uvx", "args": ["--with", "dep", "pkg"]}, "pkg"),
        ({"command": "uvx", "args": ["mcp-server-fetch==1.0"]}, "mcp-server-fetch"),
        ({"command": "uvx", "args": ["--from", "pkg-a", "run-it"]}, "pkg-a"),
        ({"command": "uvx", "args": ["./local/path"]}, "uvx"),
        ({"command": "pnpm", "args": ["dlx", "@scope/pkg"]}, "@scope/pkg"),
        ({"command": "npm", "args": ["exec", "--", "pkg"]}, "pkg"),
        ({"command": "/usr/local/bin/my-server", "args": ["--token", "secret"]}, "my-server"),
        ({"command": "npx", "args": ["https://user:pw@host.example/pkg.tgz"]}, "https://host.example/pkg.tgz"),
        ({"command": "docker", "args": ["run", "-e", "KEY=v", "--rm", "-i", "ghcr.io/x/y:1"]}, "ghcr.io/x/y"),
        ({"command": "docker", "args": ["run", "-e", "TOKEN", "secret", "img"]}, "docker"),
        ({"command": "podman", "args": ["run", "--name", "n", "reg.example:5000/team/img@sha256:abc"]},
         "reg.example:5000/team/img"),
        ({"command": "docker", "args": ["compose", "up"]}, "docker"),
        ({}, None),
    ],
)
def test_app_identity_names_the_server_not_its_launch_details(config, expected):
    assert app_identity(config) == expected


def test_interpreter_servers_get_distinct_identities_without_a_home_directory(monkeypatch):
    monkeypatch.setenv("HOME", "/opt/some-user-home")
    first = app_identity({"command": "node", "args": ["/Users/alice/work/a/build/index.js", "--key", "k"]})
    second = app_identity({"command": "node", "args": ["/Users/alice/work/b/dist/index.js"]})
    own_home = app_identity({"command": "node", "args": ["/opt/some-user-home/srv/index.js"]})

    assert first == "~/work/a/build/index.js"
    assert second == "~/work/b/dist/index.js"
    assert own_home == "~/srv/index.js"
    assert app_identity({"command": "node", "args": ["--require", "./shim.js", "/srv/app/index.js"]}) == (
        "/srv/app/index.js"
    )
    assert app_identity({"command": "python3", "args": ["-m", "mcp_server_git"]}) == "mcp_server_git"
    assert app_identity({"command": "node", "args": ["--api-key", "secret"]}) == "node"


def test_the_registry_keeps_the_stamp_through_the_reminder_and_hands_it_on(monkeypatch):
    from tools.registry import ToolRegistry

    wrapper = _wrapper("alias", "search_issues", _REMOTE)
    wrapper.mcp_app_id = "app-id"
    registry = ToolRegistry()
    registry.register(wrapper)

    async def context():
        return {"id": "thread"}, None

    async def authorized(tool, _name, model_params, _execution_params, _thread_context):
        return await tool.execute(**model_params)

    async def reachable(_tool, _context):
        return None

    async def reminder(result, _name, _params):
        assert type(result) is str  # a stamped result still earns the reminder as plain text
        return f"{result}\n\nreminder"

    monkeypatch.setattr(registry, "_load_thread_context", context)
    monkeypatch.setattr(registry, "_authorized_execute", authorized)
    monkeypatch.setattr("tools.registry.mcp_reach_refusal", reachable)
    monkeypatch.setattr("tools.registry.remind_current_turn", reminder)

    result = asyncio.run(registry.execute(wrapper.name, {}))

    assert result == "result text\n\nreminder"
    assert result_stamp(result) == {"mcp_app": "https://mcp.example.com/v1", "mcp_tool": "search_issues"}
