"""Local MCP applications use a scope-neutral Safehouse direct-exec launch."""
from __future__ import annotations

import json
import stat
from contextlib import AsyncExitStack
from pathlib import Path

import pytest

from tools import mcp_import
from tools.mcp import connect_mcp_servers
from tools.registry import ToolRegistry


@pytest.fixture
def store(tmp_path, monkeypatch) -> Path:
    """Give the key store a disposable root for synthetic MCP credentials."""
    from infra import config

    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr(config, "STATE_ROOT", root)
    return root


_NODE_MCP_SERVER = r'''#!/usr/bin/env node
const fs = require("node:fs");
const readline = require("node:readline");

function result(id, value) {
  process.stdout.write(JSON.stringify({jsonrpc: "2.0", id, result: value}) + "\n");
}
function report() {
  let write = "not-attempted";
  if (process.env.CORSIA_MCP_DENIED_WRITE) {
    try {
      fs.writeFileSync(process.env.CORSIA_MCP_DENIED_WRITE, "blocked");
      write = "wrote";
    } catch (_error) {
      write = "denied";
    }
  }
  return JSON.stringify({argv: process.argv, hasSecret: Boolean(process.env.SERVICE_TOKEN), write});
}
readline.createInterface({input: process.stdin}).on("line", (line) => {
  const request = JSON.parse(line);
  if (request.method === "initialize") {
    result(request.id, {protocolVersion: "2024-11-05", capabilities: {tools: {}}, serverInfo: {name: "npx-fixture", version: "1"}});
  } else if (request.method === "tools/list") {
    result(request.id, {tools: [{name: "inspect", description: "inspect launch", inputSchema: {type: "object", properties: {}}}]});
  } else if (request.method === "tools/call") {
    result(request.id, {content: [{type: "text", text: report()}]});
  }
});
'''

_PYTHON_MCP_SERVER = r'''import json
import os
import sys


def respond(request_id, value):
    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": value}), flush=True)


def report():
    write = "not-attempted"
    target = os.environ.get("CORSIA_MCP_DENIED_WRITE")
    if target:
        try:
            with open(target, "w", encoding="utf-8") as output:
                output.write("blocked")
            write = "wrote"
        except OSError:
            write = "denied"
    return json.dumps({"argv": sys.argv, "hasSecret": bool(os.environ.get("SERVICE_TOKEN")), "write": write})


for line in sys.stdin:
    request = json.loads(line)
    if request.get("method") == "initialize":
        respond(request["id"], {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "uvx-fixture", "version": "1"}})
    elif request.get("method") == "tools/list":
        respond(request["id"], {"tools": [{"name": "inspect", "description": "inspect launch", "inputSchema": {"type": "object", "properties": {}}}]})
    elif request.get("method") == "tools/call":
        respond(request["id"], {"content": [{"type": "text", "text": report()}]})
'''


@pytest.fixture
def local_app_workspace(monkeypatch, tmp_path) -> Path:
    """Isolate the profile builders without needing a runnable Safehouse."""
    from infra import config
    from tools import shell_guard

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(shell_guard, "_safehouse_executable", lambda: tmp_path / "safehouse")
    monkeypatch.setattr(shell_guard, "list_registered_repo_policies", lambda: [])
    monkeypatch.setattr(
        shell_guard, "get_sandbox_add_dirs_rw", lambda *_args, **_kwargs: [workspace],
    )
    monkeypatch.setattr(shell_guard, "get_allowed_dirs_ro", lambda **_kwargs: [])

    def agent_scope_is_forbidden(*_args, **_kwargs):
        raise AssertionError("a local MCP app must not receive agent-account scope")

    monkeypatch.setattr(shell_guard, "agent_account_ro_dirs", agent_scope_is_forbidden)
    monkeypatch.setattr(shell_guard, "agent_account_deny_rule", agent_scope_is_forbidden)
    return workspace


def test_local_mcp_safehouse_exec_preserves_argv_and_env_scope(local_app_workspace):
    from tools.mcp import _safehouse_stdio_parameters

    secret = "synthetic-local-mcp-secret"
    params, profile_path = _safehouse_stdio_parameters(
        "/usr/local/bin/local-mcp",
        ["--literal", "two words"],
        {"SERVICE_TOKEN": secret, "LOG_LEVEL": "debug"},
    )
    try:
        argv = [params.command, *params.args]
        separator = argv.index("--")
        passed_env = next(item for item in argv if item.startswith("--env-pass="))

        assert argv[separator + 1:] == ["/usr/local/bin/local-mcp", "--literal", "two words"]
        assert "/bin/bash" not in argv and "-c" not in argv
        assert secret not in argv
        assert {"SERVICE_TOKEN", "LOG_LEVEL", "npm_config_cache", "UV_CACHE_DIR", "XDG_CACHE_HOME"} <= set(
            passed_env.removeprefix("--env-pass=").split(",")
        )
        assert params.env["SERVICE_TOKEN"] == secret
        assert params.cwd == local_app_workspace
        assert params.env["npm_config_cache"] == str(local_app_workspace / ".corsia/mcp-cache/npm")
        assert params.env["UV_CACHE_DIR"] == str(local_app_workspace / ".corsia/mcp-cache/uv")
        assert params.env["XDG_CACHE_HOME"] == str(local_app_workspace / ".corsia/mcp-cache/xdg")
    finally:
        profile_path.unlink(missing_ok=True)


def test_local_mcp_safehouse_missing_fails_closed(monkeypatch, local_app_workspace):
    from tools import shell_guard
    from tools.mcp import _SAFEHOUSE_MCP_ERROR, _safehouse_stdio_parameters
    from tools.mcp_config import MCPConfigError

    def unavailable():
        raise FileNotFoundError

    monkeypatch.setattr(shell_guard, "_safehouse_executable", unavailable)
    with pytest.raises(MCPConfigError, match=_SAFEHOUSE_MCP_ERROR):
        _safehouse_stdio_parameters("local-mcp", [], {})


@pytest.mark.asyncio
async def test_local_mcp_missing_safehouse_never_starts_a_server(
    monkeypatch, local_app_workspace, isolated_db,
):
    from tools import shell_guard
    from tools.mcp import _SAFEHOUSE_MCP_ERROR

    def unavailable():
        raise FileNotFoundError

    monkeypatch.setattr(shell_guard, "_safehouse_executable", unavailable)
    registry = ToolRegistry()
    async with AsyncExitStack() as stack:
        errors = await connect_mcp_servers({"local": {"command": "local-mcp"}}, registry, stack)

    assert errors == {"local": _SAFEHOUSE_MCP_ERROR}
    assert registry.tool_names == []


def _write_npx_fixture(workspace: Path) -> None:
    binary = workspace / "node_modules" / ".bin" / "corsia-npx-mcp"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(_NODE_MCP_SERVER, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)


def _write_uvx_fixture(workspace: Path) -> Path:
    fixture = workspace / ".corsia" / "uvx-mcp-fixture"
    fixture.mkdir(parents=True, exist_ok=True)
    (fixture / "pyproject.toml").write_text(
        """[build-system]\nrequires = [\"setuptools>=61\"]\nbuild-backend = \"setuptools.build_meta\"\n\n[project]\nname = \"corsia-uvx-mcp-fixture\"\nversion = \"0.0.1\"\n\n[project.scripts]\ncorsia-uvx-mcp = \"corsia_uvx_mcp:main\"\n""",
        encoding="utf-8",
    )
    (fixture / "corsia_uvx_mcp.py").write_text(
        _PYTHON_MCP_SERVER + "\n\ndef main():\n    pass\n",
        encoding="utf-8",
    )
    return fixture


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("server_name, command, args_builder", [
    ("npx", "npx", lambda _workspace: ["--no-install", "corsia-npx-mcp"]),
    ("uvx", "uvx", lambda workspace: ["--from", str(_write_uvx_fixture(workspace)), "corsia-uvx-mcp"]),
])
async def test_local_mcp_package_launches_under_safehouse(
    isolated_db, store, tmp_path, server_name, command, args_builder,
):
    """L3 proof: package runners start a local app without escaping its profile."""
    from infra.config import WORKSPACE
    from infra.settings import get_mcp_servers

    workspace = Path(WORKSPACE)
    if server_name == "npx":
        _write_npx_fixture(workspace)
    blocked_target = tmp_path / "outside-safehouse-rw" / "must-not-exist"
    secret = "synthetic-local-mcp-secret"
    config = {
        "command": command,
        "args": args_builder(workspace),
        "env": {
            "SERVICE_TOKEN": secret,
            "CORSIA_MCP_DENIED_WRITE": str(blocked_target),
        },
    }
    mcp_import.save_import({server_name: config})
    saved = get_mcp_servers()[server_name]

    registry = ToolRegistry()
    async with AsyncExitStack() as stack:
        errors = await connect_mcp_servers({server_name: saved}, registry, stack)
        assert errors == {}
        report = json.loads(await registry.get(f"mcp_{server_name}_inspect").execute())

    assert report["hasSecret"] is True
    assert secret not in report["argv"]
    assert report["write"] == "denied"
    assert not blocked_target.exists()
