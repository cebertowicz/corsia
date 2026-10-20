"""Real Safehouse proof for local stdio MCP applications."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

from tools.mcp import _mcp_denial_guidance, _mcp_oserror_detail, connect_mcp_servers
from tools.registry import ToolRegistry


_PYTHON_SERVER = r'''
import json, os, signal, subprocess, sys, tempfile

def respond(request_id, result):
    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)

def text(value, *, is_error=False):
    return {"content": [{"type": "text", "text": value}], "isError": is_error}

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        respond(request["id"], {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "mcp-floor", "version": "1"}})
    elif method == "tools/list":
        respond(request["id"], {"tools": [
            {"name": "read_private", "description": "probe private files", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "read_private_link", "description": "probe linked private files", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "manage_child", "description": "spawn signal and wait", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "write_shell_envelope", "description": "attempt generated profile rewrite", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "write_path", "description": "attempt a configured cache write", "inputSchema": {"type": "object", "properties": {}}},
        ]})
    elif method == "tools/call":
        name = request["params"]["name"]
        if name == "manage_child":
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            child.send_signal(signal.SIGTERM)
            respond(request["id"], text(json.dumps({"returncode": child.wait(timeout=5)})))
        elif name in {"write_shell_envelope", "write_path"}:
            target = os.environ["MCP_ENVELOPE_WRITE_TARGET" if name == "write_shell_envelope" else "MCP_WRITE_TARGET"]
            try:
                open(target, "w", encoding="utf-8").write("(allow default)")
            except OSError as error:
                respond(request["id"], text(str(error), is_error=True))
            else:
                respond(request["id"], text(f"wrote: {target}"))
        else:
            reports = []
            denied = False
            paths = (os.environ["MCP_KEY_PATH"], os.environ["MCP_DB_PATH"])
            if name == "read_private_link":
                paths = (os.environ["MCP_KEY_LINK_PATH"], os.environ["MCP_DB_LINK_PATH"])
            for path in paths:
                try:
                    reports.append({"path": path, "content": open(path, encoding="utf-8").read()})
                except OSError as error:
                    denied = True
                    reports.append({"path": path, "errno": error.errno, "message": str(error)})
            respond(request["id"], text(json.dumps(reports), is_error=denied))
'''

_NODE_SERVER = r'''
const rl = require("node:readline").createInterface({input: process.stdin});
const reply = (id, result) => process.stdout.write(JSON.stringify({jsonrpc: "2.0", id, result}) + "\n");
rl.on("line", line => { const request = JSON.parse(line);
  if (request.method === "initialize") reply(request.id, {protocolVersion: "2024-11-05", capabilities: {tools:{}}, serverInfo:{name:"mcp-floor",version:"1"}});
  else if (request.method === "tools/list") reply(request.id, {tools:[{name:"ok",description:"answer",inputSchema:{type:"object",properties:{}}}]});
  else if (request.method === "tools/call") reply(request.id, {content:[{type:"text",text:"ok"}]});
});
'''


def _require_safehouse() -> None:
    from tools.shell_guard import safehouse_version_error

    error = safehouse_version_error()
    if error:
        if __import__("os").environ.get("CORSIA_REQUIRE_SAFEHOUSE"):
            pytest.fail(error)
        pytest.skip(error)


def _configure_roots(monkeypatch, tmp_path: Path, *, symlinked: bool = False) -> tuple[Path, Path]:
    from infra import config

    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    if symlinked:
        lexical = tmp_path / "state-link"
        lexical.symlink_to(state, target_is_directory=True)
        state_root = lexical
    else:
        state_root = state
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(config, "STATE_ROOT", state_root)
    return workspace, state


def test_mcp_profile_denies_lexical_and_resolved_state_roots(monkeypatch, tmp_path):
    from tools.safehouse_launch import mcp_private_state_profile

    _workspace, state = _configure_roots(monkeypatch, tmp_path, symlinked=True)
    lexical = tmp_path / "state-link"
    profile = mcp_private_state_profile(
        tmp_path / "generated.sb", Path("/usr/local/bin/safehouse"), lexical, tmp_path / "mcp-cache" / "app",
    )

    assert profile.startswith("(allow default)\n")
    assert f'(subpath "{lexical}")' in profile
    assert f'(subpath "{state}")' in profile
    assert f'(subpath "{state / "keys"}")' in profile
    for prefix in ("corsia-shell-envelope-", "corsia-controlled-git-", "corsia-mcp-envelope-"):
        assert f'^/tmp/{prefix}[^/]*\\.sb$' in profile
        assert f'^/private/tmp/{prefix}[^/]*\\.sb$' in profile
    from tools.managed_runtime import runtime_cache_root

    assert f'(deny file-write* (subpath "{runtime_cache_root()}"))' in profile
    assert '(deny process-info*)' in profile
    assert '(allow signal (target same-sandbox))' in profile


def test_mcp_profile_denies_the_app_cache_root_but_allows_own_cache(monkeypatch, tmp_path):
    from tools.mcp_local_transport import mcp_cache_root
    from tools.safehouse_launch import mcp_private_state_profile

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cache_root = mcp_cache_root()
    own_cache = cache_root / "app-a"
    profile = mcp_private_state_profile(
        tmp_path / "generated.sb", tmp_path / "safehouse", tmp_path / "state", own_cache,
    )

    assert f'(deny file-write* (subpath "{cache_root}"))' in profile
    assert f'(allow file-write* (subpath "{own_cache}"))' in profile


@pytest.mark.parametrize("name", ["..", "../..", "app-a/nested", "/tmp/elsewhere"])
def test_app_cache_must_be_one_directory_inside_the_cache_root(monkeypatch, tmp_path, name):
    from tools.mcp_config import MCPConfigError
    from tools.mcp_local_transport import _mcp_cache_dir

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    with pytest.raises(MCPConfigError, match="one directory inside the MCP cache root"):
        _mcp_cache_dir(name, tmp_path / "workspace", tmp_path / "state")


def test_agent_shell_profile_denies_local_app_cache_parent(monkeypatch, tmp_path):
    from infra import config
    from tools.host_path_policy import current_protected_host_path_policy
    from tools.mcp_local_transport import mcp_cache_root

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(config, "STATE_ROOT", tmp_path / "state")
    policy = current_protected_host_path_policy()

    assert f'(deny file-write* (subpath "{mcp_cache_root()}"))' in policy.sandbox_rules()


def test_mcp_envelope_dir_is_agent_shell_denied(monkeypatch, tmp_path):
    from tools.host_path_policy import ProtectedHostPathPolicy
    from tools.safehouse_launch import mcp_envelope_dir

    _workspace, state = _configure_roots(monkeypatch, tmp_path)
    envelope_dir = mcp_envelope_dir(state)
    policy = ProtectedHostPathPolicy(home=tmp_path / "home", state_root=state)

    assert envelope_dir.is_relative_to(state)
    assert envelope_dir.stat().st_mode & 0o777 == 0o700
    assert policy.ordinary_denial(envelope_dir / "profile.sb", for_write=True)
    assert f'(subpath "{state}")' in policy.sandbox_rules()


def test_mcp_direct_safehouse_argv_excludes_shell_feature_enables(tmp_path):
    from tools.safehouse_launch import direct_safehouse_argv

    argv = direct_safehouse_argv(
        "server",
        [],
        tmp_path / "cwd",
        tmp_path / "profile.sb",
        env_pass_names=set(),
        executable=lambda: tmp_path / "safehouse",
    )

    assert not any(argument.startswith("--enable=") for argument in argv)


def test_mcp_safehouse_bare_operation_not_permitted_preserves_oserror_text():
    error = OSError("open: Operation not permitted")

    assert _mcp_denial_guidance(str(error)) is None
    assert _mcp_oserror_detail(error) == str(error)


def test_mcp_safehouse_path_denial_names_private_state_recovery(monkeypatch):
    monkeypatch.setattr(
        "tools.host_path_policy.current_protected_host_path_policy",
        lambda: SimpleNamespace(
            denial_guidance_for_paths=lambda _paths: "/state/keys/value — requires reviewed host access",
        ),
    )

    assert _mcp_denial_guidance("denied") == (
        "/state/keys/value — CORSIA private state is blocked for local MCP apps. "
        "Nothing was returned. Use the app's own working directory or a supported "
        "non-private integration."
    )


@pytest.mark.parametrize("template", [
    "[Errno 1] Operation not permitted: '{path}'",
    "EPERM: operation not permitted, open '{path}'",
])
@pytest.mark.parametrize("embedded", [False, True])
def test_mcp_safehouse_native_error_paths_are_guided_only_when_private(
    monkeypatch, tmp_path, template, embedded,
):
    _workspace, state = _configure_roots(monkeypatch, tmp_path)
    private_path = state / "corsia.db"
    non_private_path = tmp_path / "ordinary.txt"

    def render(path: Path) -> str:
        error = template.format(path=path)
        return json.dumps({"error": error}) if embedded else error

    private_guidance = _mcp_denial_guidance(render(private_path))
    assert private_guidance is not None
    assert "CORSIA private state is blocked for local MCP apps" in private_guidance
    assert _mcp_denial_guidance(render(non_private_path)) is None


@pytest.mark.asyncio
async def test_missing_safehouse_refuses_stdio_without_spawning(monkeypatch, tmp_path):
    from tools import shell_guard

    _configure_roots(monkeypatch, tmp_path)
    marker = tmp_path / "spawned"
    app = tmp_path / "app.py"
    app.write_text(f'from pathlib import Path; Path({str(marker)!r}).write_text("ran")\n', encoding="utf-8")
    monkeypatch.setattr(shell_guard, "safehouse_version_error", lambda: "could not verify the Agent Safehouse version: Agent Safehouse is unavailable")

    async with AsyncExitStack() as stack:
        errors = await connect_mcp_servers(
            {"local": {"command": sys.executable, "args": [str(app)]}}, ToolRegistry(), stack,
        )
    assert "Agent Safehouse was not found" in errors["local"]
    assert not marker.exists()


async def _connect(name: str, config: dict):
    registry = ToolRegistry()
    async with AsyncExitStack() as stack:
        errors = await connect_mcp_servers({name: config}, registry, stack)
        assert errors == {}
        return registry


@pytest.mark.integration
@pytest.mark.asyncio
async def test_local_mcp_safehouse_blocks_private_state_and_allows_child_management(monkeypatch, tmp_path):
    _require_safehouse()
    workspace, state = _configure_roots(monkeypatch, tmp_path, symlinked=True)
    keys = state / "keys"
    keys.mkdir()
    secret = "mcp-secret-value"
    key_path = keys / "value"
    key_path.write_text(secret, encoding="utf-8")
    db_path = state / "corsia.db"
    db_path.write_text("database-private", encoding="utf-8")
    app = tmp_path / "server.py"
    app.write_text(_PYTHON_SERVER, encoding="utf-8")
    config = {"command": sys.executable, "args": [str(app)], "env": {
        "MCP_KEY_PATH": str(key_path), "MCP_DB_PATH": str(db_path),
        "MCP_KEY_LINK_PATH": str(tmp_path / "state-link" / "keys" / "value"),
        "MCP_DB_LINK_PATH": str(tmp_path / "state-link" / "corsia.db"),
        "MCP_ENVELOPE_WRITE_TARGET": str(
            Path(tempfile.gettempdir()) / "corsia-shell-envelope-mcp-test.sb",
        ),
    }}

    registry = ToolRegistry()
    async with AsyncExitStack() as stack:
        errors = await connect_mcp_servers({"private": config}, registry, stack)
        assert errors == {}
        read_result = await registry.get("mcp_private_read_private").execute()
        link_read_result = await registry.get("mcp_private_read_private_link").execute()
        child_result = await registry.get("mcp_private_manage_child").execute()
        rewrite_result = await registry.get("mcp_private_write_shell_envelope").execute()
    assert secret not in read_result
    assert secret not in link_read_result
    assert "CORSIA private state is blocked for local MCP apps" in read_result or "Stored secrets are closed" in read_result
    assert "CORSIA private state is blocked for local MCP apps" in link_read_result or "Stored secrets are closed" in link_read_result
    assert json.loads(child_result)["returncode"] == -15
    assert "Operation not permitted" in rewrite_result


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("name,command,args", [
    ("python", sys.executable, ["{python}"]),
    ("node", "node", ["{node}"]),
    ("npx", "npx", ["--no-install", "mcp-floor-npx"]),
])
async def test_local_mcp_safehouse_real_launches(monkeypatch, tmp_path, name, command, args):
    _require_safehouse()
    if shutil.which(command) is None:
        pytest.fail(f"{command} is required for the L3 Safehouse proof")
    workspace, state = _configure_roots(monkeypatch, tmp_path)
    python_app = tmp_path / "server.py"
    node_app = tmp_path / "server.js"
    python_app.write_text(_PYTHON_SERVER, encoding="utf-8")
    node_app.write_text(_NODE_SERVER, encoding="utf-8")
    node_app.chmod(node_app.stat().st_mode | stat.S_IXUSR)
    if name == "npx":
        from tools.mcp_local_transport import _mcp_app_id, _mcp_cache_dir
        cache = _mcp_cache_dir(_mcp_app_id(name), workspace, state) / "cwd"
        package = cache / "node_modules" / "mcp-floor-npx"
        binary = package / "bin" / "server.js"
        binary.parent.mkdir(parents=True)
        (package / "package.json").write_text('{"name":"mcp-floor-npx","version":"1","bin":"bin/server.js"}', encoding="utf-8")
        binary.write_text("#!/usr/bin/env node\n" + _NODE_SERVER, encoding="utf-8")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        link = cache / "node_modules" / ".bin" / "mcp-floor-npx"
        link.parent.mkdir(parents=True)
        link.symlink_to(Path("..") / "mcp-floor-npx" / "bin" / "server.js")
    rendered_args = [arg.format(python=python_app, node=node_app) for arg in args]
    registry = await _connect(name, {"command": command, "args": rendered_args})
    assert await registry.get(f"mcp_{name}_{'ok' if name != 'python' else 'manage_child'}").execute()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_local_mcp_safehouse_blocks_cross_app_cache_writes(monkeypatch, tmp_path):
    _require_safehouse()
    workspace, state = _configure_roots(monkeypatch, tmp_path)
    from tools.mcp_local_transport import _mcp_app_id, _mcp_cache_dir

    app = tmp_path / "server.py"
    app.write_text(_PYTHON_SERVER, encoding="utf-8")
    app_a_cache = _mcp_cache_dir(_mcp_app_id("app_a"), workspace, state)
    registry = ToolRegistry()
    async with AsyncExitStack() as stack:
        errors = await connect_mcp_servers(
            {
                "app_a": {"command": sys.executable, "args": [str(app)], "env": {
                    "MCP_WRITE_TARGET": str(app_a_cache / "self-write"),
                }},
                "app_b": {"command": sys.executable, "args": [str(app)], "env": {
                    "MCP_WRITE_TARGET": str(app_a_cache / "cross-write"),
                }},
            },
            registry,
            stack,
        )
        assert errors == {}
        assert "wrote:" in await registry.get("mcp_app_a_write_path").execute()
        assert "Operation not permitted" in await registry.get("mcp_app_b_write_path").execute()
    assert (app_a_cache / "self-write").exists()
    assert not (app_a_cache / "cross-write").exists()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_runtime_connects_each_app_separately_but_blocks_all_other_caches(monkeypatch, tmp_path):
    """Production's one-app-at-a-time path cannot weaken app-cache isolation."""
    _require_safehouse()
    workspace, state = _configure_roots(monkeypatch, tmp_path)
    from infra import state as infra_state
    from tools import mcp_runtime
    from tools.mcp_local_transport import _mcp_cache_dir
    from tools.registry import ToolRegistry

    app = tmp_path / "server.py"
    app.write_text(_PYTHON_SERVER, encoding="utf-8")
    app_a_cache = _mcp_cache_dir("app-a", workspace, state)
    never_connected_cache = _mcp_cache_dir("never-connected", workspace, state)
    configs = {
        "app-a": ("app_a", {"connected_app_id": "app-a", "command": sys.executable, "args": [str(app)], "env": {
            "MCP_WRITE_TARGET": str(app_a_cache / "self-write"),
        }}),
        "app-b": ("app_b", {"connected_app_id": "app-b", "command": sys.executable, "args": [str(app)], "env": {
            "MCP_WRITE_TARGET": str(app_a_cache / "cross-write"),
        }}),
    }

    async def run_in_db(fn, *args):
        if fn.__name__ == "load_runtime_app_config":
            return configs[args[0]]
        if fn.__name__ == "load_missing_app_key_refs":
            return []
        if fn.__name__ == "record_runtime_app_state":
            return None
        raise AssertionError(fn.__name__)

    monkeypatch.setattr(mcp_runtime, "_lock", asyncio.Lock())
    monkeypatch.setattr(mcp_runtime, "_registry", ToolRegistry())
    monkeypatch.setattr(mcp_runtime, "_app_stacks", {})
    monkeypatch.setattr(mcp_runtime, "_app_tools", {})
    monkeypatch.setattr(mcp_runtime, "_app_servers", {})
    monkeypatch.setattr(mcp_runtime, "_app_configs", {})
    monkeypatch.setattr(mcp_runtime, "_app_states", {})
    monkeypatch.setattr(mcp_runtime, "_reconnect_at", {})
    monkeypatch.setattr(mcp_runtime, "_stopped", False)
    monkeypatch.setattr(mcp_runtime, "run_in_db", run_in_db)
    monkeypatch.setattr(infra_state, "_agent", None)

    try:
        assert (await mcp_runtime.apply_connected_app_runtime("connect", "app-a"))["state"] == "working"
        assert "wrote:" in await mcp_runtime._registry.get("mcp_app_a_write_path").execute()
        assert (await mcp_runtime.apply_connected_app_runtime("connect", "app-b"))["state"] == "working"
        denied_a = await mcp_runtime._registry.get("mcp_app_b_write_path").execute()
        configs["app-b"][1]["env"]["MCP_WRITE_TARGET"] = str(never_connected_cache / "cross-write")
        assert (await mcp_runtime.apply_connected_app_runtime("restart", "app-b"))["state"] == "working"
        denied_never = await mcp_runtime._registry.get("mcp_app_b_write_path").execute()
    finally:
        await mcp_runtime.stop_mcp_runtime()

    assert "Operation not permitted" in denied_a
    assert "Operation not permitted" in denied_never
    assert (app_a_cache / "self-write").exists()
    assert not (app_a_cache / "cross-write").exists()
    assert not (never_connected_cache / "cross-write").exists()


@pytest.mark.integration
@pytest.mark.parametrize("target_name", ("runtime", "launch-parent", "launch-tree"))
def test_agent_shell_safehouse_refuses_runtime_and_launch_cache_writes(monkeypatch, tmp_path, target_name):
    _require_safehouse()
    from tools.managed_runtime import runtime_cache_root
    from tools.mcp_local_transport import mcp_cache_root
    from tools.shell_guard import _terminal_boundary_profile, safehouse_argv

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    launch_parent = mcp_cache_root()
    launch_tree = launch_parent / "app-a"
    runtime = runtime_cache_root()
    for directory in (launch_parent, launch_tree, runtime):
        directory.mkdir(parents=True, exist_ok=True)
    targets = {
        "runtime": runtime / "agent-write",
        "launch-parent": launch_parent / "agent-write",
        "launch-tree": launch_tree / "agent-write",
    }
    profile_path = tmp_path / "agent.sb"
    profile_path.write_text(_terminal_boundary_profile(profile_path, workspace, leased=False), encoding="utf-8")

    result = subprocess.run(
        safehouse_argv(f'printf denied > "{targets[target_name]}"', workspace, profile_path),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "Operation not permitted" in result.stdout + result.stderr
    assert not targets[target_name].exists()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_local_mcp_safehouse_uvx_launch(monkeypatch, tmp_path):
    _require_safehouse()
    # Ignore any runner uvx so this launch proves C6 fetches its own pinned,
    # checksum-verified uv runtime without a test-specific CI installation.
    _configure_roots(monkeypatch, tmp_path)
    from tools import safehouse_launch
    from tools.managed_runtime import runtime_cache_root

    launched = []
    direct_launch = safehouse_launch.direct_safehouse_argv

    def capture_launch(command, *args, **kwargs):
        launched.append(Path(command).resolve(strict=False))
        return direct_launch(command, *args, **kwargs)

    monkeypatch.setattr(safehouse_launch, "direct_safehouse_argv", capture_launch)
    package = tmp_path / "uvx-package"
    package.mkdir()
    (package / "pyproject.toml").write_text(
        "[build-system]\nrequires=['setuptools']\nbuild-backend='setuptools.build_meta'\n"
        "[project]\nname='mcp-floor-uvx'\nversion='1'\n[project.scripts]\nmcp-floor-uvx='mcp_floor_uvx:main'\n",
        encoding="utf-8",
    )
    (package / "mcp_floor_uvx.py").write_text(_PYTHON_SERVER + "\ndef main():\n    pass\n", encoding="utf-8")
    registry = await _connect("uvx", {
        "command": "uvx",
        "args": ["--from", str(package), "mcp-floor-uvx"],
        "env": {"PATH": str(tmp_path / "no-host-uvx")},
    })
    assert await registry.get("mcp_uvx_manage_child").execute()
    assert len(launched) == 1
    assert launched[0].name == "uvx"
    assert launched[0].is_relative_to(runtime_cache_root())
