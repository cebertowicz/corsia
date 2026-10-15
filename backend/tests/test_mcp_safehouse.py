"""Local MCP applications use a scope-neutral Safehouse direct-exec launch."""
from __future__ import annotations

import asyncio
import errno
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
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
  let write = {result: "not-attempted"};
  if (process.env.CORSIA_MCP_DENIED_WRITE) {
    try {
      fs.writeFileSync(process.env.CORSIA_MCP_DENIED_WRITE, "blocked");
      write = {result: "wrote"};
    } catch (error) {
      write = {result: "denied", code: error.code, errno: error.errno};
    }
  }
  return JSON.stringify({argv: process.argv, cwd: process.cwd(), hasSecret: Boolean(process.env.SERVICE_TOKEN), write});
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

_PYTHON_MCP_SERVER = r'''import errno
import json
import os
import sys


def respond(request_id, value):
    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": value}), flush=True)


def report():
    write = {"result": "not-attempted"}
    target = os.environ.get("CORSIA_MCP_DENIED_WRITE")
    if target:
        try:
            with open(target, "w", encoding="utf-8") as output:
                output.write("blocked")
            write = {"result": "wrote"}
        except OSError as error:
            write = {"result": "denied", "code": errno.errorcode.get(error.errno), "errno": error.errno}
    return json.dumps({"argv": sys.argv, "cwd": os.getcwd(), "hasSecret": bool(os.environ.get("SERVICE_TOKEN")), "write": write})


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
    from tools import mcp

    monkeypatch.setattr(mcp.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(shell_guard, "_safehouse_executable", lambda: tmp_path / "safehouse")
    monkeypatch.setattr(shell_guard, "list_registered_repo_policies", lambda: [])
    monkeypatch.setattr(
        shell_guard, "get_sandbox_add_dirs_rw", lambda *_args, **_kwargs: [workspace],
    )
    monkeypatch.setattr(shell_guard, "get_allowed_dirs_ro", lambda **_kwargs: [])

    def agent_scope_is_forbidden(*_args, **_kwargs):
        raise AssertionError("a local MCP app must not receive agent-account scope")

    monkeypatch.setattr(shell_guard, "agent_account_ro_dirs", agent_scope_is_forbidden)
    return workspace


def test_local_mcp_safehouse_exec_preserves_argv_and_env_scope(local_app_workspace):
    from tools.mcp import _safehouse_stdio_parameters

    secret = "synthetic-local-mcp-secret"
    params, profile_path = _safehouse_stdio_parameters(
        "local-app",
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
        assert {
            "SERVICE_TOKEN", "LOG_LEVEL", "NODE_OPTIONS", "npm_config_cache", "npm_config_script_shell",
            "UV_CACHE_DIR", "XDG_CACHE_HOME",
        } <= set(passed_env.removeprefix("--env-pass=").split(","))
        assert params.env["SERVICE_TOKEN"] == secret
        assert params.env["npm_config_script_shell"] == "/bin/sh"
        cache_dir = Path(params.env["npm_config_cache"]).parent
        assert params.cwd == cache_dir / "cwd"
        assert params.cwd.is_dir()
        assert cache_dir.parent.name == "mcp"
        assert not cache_dir.is_relative_to(local_app_workspace)
        assert params.env["UV_CACHE_DIR"] == str(cache_dir / "uv")
        assert params.env["XDG_CACHE_HOME"] == str(cache_dir / "xdg")
        shim_path = cache_dir / "no-proctitle.cjs"
        assert params.env["NODE_OPTIONS"] == f"--require {shim_path}"
        assert "Object.defineProperty(process, 'title', {get, set(){}, configurable:true})" in shim_path.read_text(encoding="utf-8")
        assert str(cache_dir) in next(option for option in argv if option.startswith("--add-dirs="))
    finally:
        profile_path.unlink(missing_ok=True)


def test_local_mcp_profile_keeps_personal_gh_home_denied_without_agent_account_grant(
    monkeypatch, local_app_workspace, tmp_path,
):
    from connections.github_agent import AgentAccountView
    from tools import shell_environment
    from tools.mcp import _safehouse_stdio_parameters

    person_home = tmp_path / "person-gh"
    monkeypatch.setattr(
        shell_environment.github_agent, "view",
        lambda: AgentAccountView("ready", home=str(tmp_path / "agents-gh")),
    )
    monkeypatch.setattr(shell_environment.github_agent, "default_home_deny", lambda _view: person_home)

    _params, profile_path = _safehouse_stdio_parameters("account-deny", "node", [], {})
    try:
        assert f'(deny file-read* file-write* (subpath "{person_home}"))' in profile_path.read_text(encoding="utf-8")
    finally:
        profile_path.unlink(missing_ok=True)



@pytest.mark.parametrize("existing", [None, "--conditions=development"])
def test_local_mcp_preload_prepends_existing_node_options(local_app_workspace, existing):
    from tools.mcp import _safehouse_stdio_parameters

    env = {} if existing is None else {"NODE_OPTIONS": existing}
    params, profile_path = _safehouse_stdio_parameters("node-options", "node", [], env)
    try:
        shim_path = Path(params.env["npm_config_cache"]).parent / "no-proctitle.cjs"
        expected = f"--require {shim_path}"
        assert params.env["NODE_OPTIONS"] == (expected if existing is None else f"{expected} {existing}")
    finally:
        profile_path.unlink(missing_ok=True)


def test_local_mcp_keeps_an_app_configured_npm_script_shell(local_app_workspace):
    from tools.mcp import _safehouse_stdio_parameters

    params, profile_path = _safehouse_stdio_parameters(
        "script-shell", "npx", [], {"npm_config_script_shell": "/bin/zsh"},
    )
    try:
        assert params.env["npm_config_script_shell"] == "/bin/zsh"
    finally:
        profile_path.unlink(missing_ok=True)


def test_npx_runs_its_bin_past_a_path_entry_that_stops_libuv(local_app_workspace, tmp_path):
    """libuv's PATH search for a bare `sh` aborts on any error but ENOENT, ENOTDIR or EACCES.

    Under Safehouse that error is EPERM from a denied entry; a self-referencing
    `sh` link (ELOOP) reproduces the same abort without a sandbox.
    """
    from tools.mcp import _safehouse_stdio_parameters

    npx_bin, node_bin = shutil.which("npx"), shutil.which("node")
    if npx_bin is None or node_bin is None:
        pytest.skip("npx is unavailable")
    _write_npx_fixture(tmp_path)
    poison = tmp_path / "poison"
    poison.mkdir()
    (poison / "sh").symlink_to("sh")
    params, profile_path = _safehouse_stdio_parameters("poisoned-path", "npx", [], {})
    profile_path.unlink(missing_ok=True)
    path = os.pathsep.join([str(poison), str(Path(node_bin).parent), "/usr/bin", "/bin"])
    inherited = {key: value for key, value in os.environ.items() if key.lower() != "npm_config_script_shell"}
    base = {**inherited, "PATH": path, "npm_config_cache": params.env["npm_config_cache"]}

    def npx(env: dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [npx_bin, "--no-install", "corsia-npx-mcp"], cwd=tmp_path, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, errors="replace", timeout=_CASE_TIMEOUT, check=False,
        )

    assert npx(base).returncode != 0
    fixed = npx({**base, "npm_config_script_shell": params.env["npm_config_script_shell"]})
    assert fixed.returncode == 0, fixed.stderr


def test_mcp_cache_ids_are_stable_path_safe_and_app_private(local_app_workspace):
    from tools.mcp import _mcp_app_id, _mcp_cache_dir

    state_root = local_app_workspace.parent / "state"
    app_a = _mcp_cache_dir("../../App A", local_app_workspace, state_root)
    app_b = _mcp_cache_dir("App B", local_app_workspace, state_root)
    assert app_a == _mcp_cache_dir("../../App A", local_app_workspace, state_root)
    assert app_a != app_b
    assert app_a.name == _mcp_app_id("../../App A")
    assert app_a.parent.name == "mcp"
    assert not app_a.is_relative_to(local_app_workspace)
    assert not app_a.is_relative_to(state_root)


def test_no_proctitle_preload_preserves_getter_and_blocks_nonconfigurable_descriptor(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable")
    from tools.mcp import _write_no_proctitle_shim

    shim_path = _write_no_proctitle_shim(tmp_path / "cache")
    success = subprocess.run(
        [node, "--require", str(shim_path), "-e", "const old = process.title; process.title = 'x'; process.exit(process.title === old ? 0 : 1)"],
        capture_output=True, text=True, check=False,
    )
    assert success.returncode == 0
    preamble = tmp_path / "nonconfigurable.cjs"
    preamble.write_text("Object.defineProperty(process, 'title', {get(){ return 'x'; }, set(){}, configurable:false});\n", encoding="utf-8")
    blocked = subprocess.run(
        [node, "--require", str(preamble), "--require", str(shim_path), "-e", ""],
        capture_output=True, text=True, check=False,
    )
    assert blocked.returncode != 0
    assert "cannot safely disable process.title" in blocked.stderr


def test_local_mcp_safehouse_missing_fails_closed(monkeypatch, local_app_workspace):
    from tools import shell_guard
    from tools.mcp import _SAFEHOUSE_MCP_ERROR, _safehouse_stdio_parameters
    from tools.mcp_config import MCPConfigError

    def unavailable():
        raise FileNotFoundError

    monkeypatch.setattr(shell_guard, "_safehouse_executable", unavailable)
    with pytest.raises(MCPConfigError, match=_SAFEHOUSE_MCP_ERROR):
        _safehouse_stdio_parameters("local-app", "local-mcp", [], {})


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
    """Install a true local package and bin link so npx resolves from its launch cwd."""
    package = workspace / "node_modules" / "corsia-npx-mcp"
    binary = package / "bin" / "server.js"
    binary.parent.mkdir(parents=True, exist_ok=True)
    (package / "package.json").write_text(
        '{"name":"corsia-npx-mcp","version":"0.0.1","bin":"bin/server.js"}\n', encoding="utf-8",
    )
    binary.write_text(_NODE_MCP_SERVER, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    link = workspace / "node_modules" / ".bin" / "corsia-npx-mcp"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(Path("..") / "corsia-npx-mcp" / "bin" / "server.js")


def test_npx_fixture_is_a_local_package(tmp_path):
    _write_npx_fixture(tmp_path)

    package = tmp_path / "node_modules" / "corsia-npx-mcp"
    link = tmp_path / "node_modules" / ".bin" / "corsia-npx-mcp"
    assert json.loads((package / "package.json").read_text(encoding="utf-8"))["bin"] == "bin/server.js"
    assert link.resolve() == package / "bin" / "server.js"


@pytest.mark.skipif(shutil.which("npx") is None, reason="npx is unavailable")
def test_npx_does_not_resolve_a_workspace_planted_bin_from_the_app_cwd(local_app_workspace):
    from tools.mcp import _safehouse_stdio_parameters

    params, profile_path = _safehouse_stdio_parameters("npx-private-cwd", "npx", [], {})
    marker = local_app_workspace / "workspace-bin-ran"
    try:
        _write_npx_fixture(Path(params.cwd))
        _write_npx_fixture(local_app_workspace)
        planted = local_app_workspace / "node_modules" / "corsia-npx-mcp" / "bin" / "server.js"
        planted.write_text(
            f'#!/usr/bin/env node\nrequire("node:fs").writeFileSync({json.dumps(str(marker))}, "ran");\n',
            encoding="utf-8",
        )
        result = subprocess.run(
            ["npx", "--no-install", "corsia-npx-mcp"], cwd=params.cwd,
            env={**os.environ, **params.env}, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, errors="replace", timeout=_CASE_TIMEOUT, check=False,
        )

        assert result.returncode == 0, result.stderr
        assert not marker.exists()
    finally:
        profile_path.unlink(missing_ok=True)


@pytest.mark.skipif(shutil.which("npx") is None, reason="npx is unavailable")
def test_npx_fixture_resolves_from_its_launch_workspace(tmp_path):
    _write_npx_fixture(tmp_path)

    result = subprocess.run(
        ["npx", "--no-install", "corsia-npx-mcp"], cwd=tmp_path, capture_output=True,
        text=True, errors="replace", timeout=_CASE_TIMEOUT, check=False,
    )

    assert result.returncode == 0, result.stderr


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


_CASE_TIMEOUT = 15
_NODE_EXIT = ["-e", "process.title = 'x'"]
_CRASH_DIRS = (Path.home() / "Library" / "Logs" / "DiagnosticReports", Path("/Library/Logs/DiagnosticReports"))
_SPAWN_SH = (
    "const r = require('child_process').spawnSync('/bin/sh', ['-c', 'true']);"
    "console.log(JSON.stringify({status: r.status, signal: r.signal, error: r.error && String(r.error)}));"
    "process.exit(r.status === 0 ? 0 : 1);"
)
# P1-P5 and P8: spawn routes under one app envelope; npm's own set-path builds the PATH it gives a bin.
_SPAWN_PROBE = r'''
const cp = require('child_process'), fs = require('fs'), path = require('path');
const outcome = (r) => r.error
  ? {code: r.error.code, errno: r.error.errno, syscall: r.error.syscall, path: r.error.path}
  : {status: r.status, signal: r.signal};
function npmPath() {
  for (const dir of (process.env.PATH || '').split(path.delimiter)) {
    let cli;
    try { cli = fs.realpathSync(path.join(dir, 'npx')); } catch { continue; }
    const setPath = require(path.resolve(path.dirname(cli), '..', 'node_modules', '@npmcli', 'run-script', 'lib', 'set-path.js'));
    return setPath(process.cwd(), [], {PATH: process.env.PATH}).PATH;
  }
  throw new Error('no readable npx on PATH');
}
const run = (file, args, PATH) => outcome(cp.spawnSync(file, args, {env: {...process.env, PATH}, stdio: 'ignore'}));
const probes = {
  P1: () => [run('/usr/bin/true', [], process.env.PATH)],
  P2: () => [run('sh', ['-c', 'true'], '/usr/bin:/bin')],
  P3: () => [run('sh', ['-c', 'true'], npmPath())],
  P4: () => [run('/bin/sh', ['-c', 'true'], npmPath())],
  P5: () => npmPath().split(path.delimiter).map((entry) => {
    let stat = 'ok';
    try { fs.statSync(entry); } catch (e) { stat = `${e.code} errno=${e.errno}`; }
    const r = cp.spawnSync(path.join(entry, 'sh'), ['-c', 'true'], {stdio: 'ignore'});
    return `${entry} stat=${stat} spawn(sh)=${r.error ? r.error.code : 'ok'}`;
  }),
  P8: () => [run('/usr/bin/env', ['node', '-e', '0'], npmPath())],
};
for (const line of probes[process.argv[1]]()) console.log(typeof line === 'string' ? line : JSON.stringify(line));
'''
_PROBE_LABELS = {
    "P1": "spawn-usr-bin-true", "P2": "sh-clean-path", "P3": "sh-npm-path",
    "P4": "bin-sh-npm-path", "P5": "npm-path-entries", "P8": "env-node-npm-path",
}
_WRITE_PROBE = (
    "const fs = require('fs'); let r;"
    "try { fs.writeFileSync(process.argv[1], 'x'); r = {result: 'wrote'}; }"
    "catch (e) { r = {result: 'error', code: e.code, errno: e.errno}; }"
    "console.log(JSON.stringify(r));"
)
# Each bisect variant drops one CORSIA option group before Safehouse's "--".
_FLAG_GROUPS = {
    "env-pass": "--env-pass=",
    "add-dirs": "--add-dirs=",
    "add-dirs-ro": "--add-dirs-ro=",
    "workdir": "--workdir=",
    "enable": "--enable=",
    "append-profile": "--append-profile=",
}


@dataclass
class _Case:
    """One launch case's outcome: PASS, FAIL or SKIP, with its exit and stderr tail."""

    name: str
    status: str = "PASS"
    returncode: int | None = None
    seconds: float = 0.0
    detail: str = ""
    stderr: list[str] = field(default_factory=list)
    stdout: list[str] = field(default_factory=list)


def _signal_of(code: int | None) -> str:
    """Name a signal from a direct exit or from a shell's 128+N report of its child."""
    if code is None:
        return "-"
    try:
        name = signal.Signals(-code if code < 0 else code - 128).name
    except ValueError:
        return "none"
    return name if code < 0 else f"{name} via shell"


def _tail(text: str | None, secret: str = "", keep: int = 20) -> list[str]:
    lines = (text or "").splitlines()[-keep:]
    return [line.replace(secret, "<secret>") for line in lines] if secret else lines


def _run_raw(
    name: str, argv: list[str], env: dict[str, str], cwd: Path, keep: int = 20, timeout: int = _CASE_TIMEOUT,
) -> _Case:
    """Launch one command in its own session and keep its exit, stdout and stderr."""
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, errors="replace", start_new_session=True,
        )
    except OSError as error:
        return _Case(name, "FAIL", detail=f"spawn failed: {error}")
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        timed_out = True
    output = _tail(stdout, keep=keep)
    detail = f"timed out after {timeout}s" if timed_out else f"stdout: {output[-1][:160]}" if output else ""
    status = "PASS" if process.returncode == 0 and not timed_out else "FAIL"
    return _Case(
        name, status, process.returncode, time.monotonic() - started, detail, _tail(stderr, keep=keep), output,
    )


def _host_output(argv: list[str]) -> str:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=_CASE_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"<{error}>"
    return (done.stdout or done.stderr).strip() or f"<exit {done.returncode}>"


def _host_binaries() -> list[str]:
    """Show, outside any sandbox, which Node and package runners the job resolves."""
    lines = []
    for tool in ("safehouse", "node", "npx", "uvx"):
        found = shutil.which(tool)
        lines.append(f"which {tool}: {found} -> {os.path.realpath(found) if found else '-'}")
    if node := shutil.which("node"):
        exec_path = _host_output([node, "-p", "process.execPath"])
        real = os.path.realpath(exec_path)
        lines += [
            f"node --version: {_host_output([node, '--version'])}",
            f"node -p process.execPath: {exec_path}",
            f"realpath(execPath): {real}",
            f"file: {_host_output(['/usr/bin/file', real])}",
            f"hostedtoolcache node: {'hostedtoolcache' in real}",
        ]
    if safehouse := shutil.which("safehouse"):
        lines.append(f"safehouse --version: {_host_output([safehouse, '--version'])}")
    return lines


def _bisect_cases(argv: list[str], env: dict[str, str], cwd: Path, policy: Path) -> list[_Case]:
    """Bisect case b: Safehouse's own /usr/bin/env -i hop, then one option group at a time."""
    split = argv.index("--")
    options, command = argv[:split], argv[split:]
    # Same launch as b, plus Safehouse's explain summary and a kept copy of its rendered policy.
    cases = [_run_raw("c-explain-render", [*options, "--explain", f"--output={policy}", *command], env, cwd, keep=40)]
    pairs = [f"{key}={value}" for key, value in env.items()]
    for name, hop in (
        ("c-env-i-kept", ["/usr/bin/env", "-i", *pairs]),
        ("c-env-i-kept+container-id", ["/usr/bin/env", "-i", *pairs, "APP_SANDBOX_CONTAINER_ID=agent-safehouse"]),
        ("c-env-i-removed", []),
    ):
        cases.append(
            _run_raw(name, ["/usr/bin/sandbox-exec", "-f", str(policy), "--", *hop, "node", *_NODE_EXIT], env, cwd)
            if policy.is_file() else _Case(name, "SKIP", detail="Safehouse rendered no policy")
        )
    variants = {f"c-no-{group}": [o for o in options if not o.startswith(flag)] for group, flag in _FLAG_GROUPS.items()}
    variants["c-env-pass-PATH-only"] = ["--env-pass=PATH" if o.startswith("--env-pass=") else o for o in options]
    for name, reduced in variants.items():
        cases.append(
            _run_raw(name, [*reduced, *command], env, cwd) if reduced != options
            else _Case(name, "SKIP", detail="option absent from CORSIA argv")
        )
    return cases


def _launch(name: str, corsia, command: str, args: list[str]) -> _Case:
    """Run one command through CORSIA's real Safehouse launch; a launch that cannot be built is a case too."""
    try:
        argv, env, cwd = corsia(command, args)
    except Exception as error:
        return _Case(name, "FAIL", detail=f"launch not built: {error!r}")
    return _run_raw(name, argv, env, cwd)


def _npx_cases(corsia) -> list[_Case]:
    """Diagnostic npx version plus shell and Node-child routes under CORSIA's flags."""
    npx = shutil.which("npx")
    version = _launch("h-npx-version", corsia, "npx", ["--version"])
    if npx is None:
        version = _Case("h-npx-version", "SKIP", detail="npx not on PATH")
    return [
        version,
        _launch("j-sh-c-node", corsia, "/bin/sh", ["-c", "node -e 0"]),
        _launch("k-node-spawnsync-sh", corsia, "node", ["-e", _SPAWN_SH]),
    ]


def _app_envelope(profiles: list[Path], name: str, command: str, args: list[str]):
    """The argv, env and app-private cwd CORSIA gives one local app."""
    from mcp.client.stdio import get_default_environment
    from tools.mcp import _safehouse_stdio_parameters

    params, profile = _safehouse_stdio_parameters(name, command, args, {})
    if command == "npx":
        _write_npx_fixture(Path(params.cwd))
    profiles.append(profile)
    return [params.command, *params.args], {**get_default_environment(), **params.env}, Path(params.cwd)


def _npx_probe_cases(profiles: list[Path]) -> list[_Case]:
    """P1-P8 (INFO): the syscall and PATH entry behind npm's bin spawn, under the envelope e uses."""

    def envelope(name: str, command: str, args: list[str]):
        return _app_envelope(profiles, name, command, args)

    cases = []
    for probe, label in _PROBE_LABELS.items():
        case = _run_raw(f"{probe}-{label}", *envelope(f"probe-{probe}", "node", ["-e", _SPAWN_PROBE, probe]))
        case.detail = " | ".join(case.stdout) or case.detail
        cases.append(case)
    # The same npx launch with and without the launcher's absolute script shell.
    argv, env, cwd = envelope("probe-npx", "npx", ["--no-install", "corsia-npx-mcp"])
    without = {key: value for key, value in env.items() if key != "npm_config_script_shell"}
    cases += [
        _run_raw("P6-npx-without-script-shell", argv, without, cwd),
        _run_raw("P7-npx-with-script-shell", argv, env, cwd),
    ]
    for case in cases:
        case.status = "INFO"
    return cases


# T1-T5: where common toolchains install an app's command, under $HOME.
_TOOLCHAIN_DIRS = {
    "T1-home-local-bin": ".local/bin",
    "T2-nvm-node-bin": ".nvm/versions/node/v0/bin",
    "T3-pyenv-version-bin": ".pyenv/versions/0/bin",
    "T4-cargo-bin": ".cargo/bin",
    "T5-arbitrary-home-app": "corsia-probe-apps/x/bin",
}
_HOMEBREW_CANDIDATES = ("bash", "git", "python3", "node", "jq")


def _exec_verdict(case: _Case) -> str:
    if case.returncode == 0:
        return "allowed"
    if any("Operation not permitted" in line for line in case.stderr):
        return "EPERM"
    return f"failed rc={case.returncode} {(case.stderr or [case.detail])[-1][:80]}"


def _build_native_probe(scratch: Path) -> Path | None:
    """Build an ad-hoc-signed binary that exits 0, or None without a compiler."""
    source = scratch / "corsia-probe.c"
    source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    binary = scratch / "corsia-probe"
    try:
        subprocess.run(["cc", "-o", str(binary), str(source)], check=True, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return binary


def _toolchain_exec_cases(workspace: Path, scratch: Path, profiles: list[Path]) -> list[_Case]:
    """T0-T6 (INFO): may an app exec a command from each common toolchain location?

    Each location gets a native binary and a #!/bin/sh script, the form npm and
    pip bins take; T0 places them in the granted workspace as a control. macOS
    kills a relocated platform binary such as a copied /bin/sh, so the native
    stand-in is built here. Homebrew is only read: T6 runs a binary already there.
    """
    native = _build_native_probe(scratch)
    targets = {"T0-control-workspace": workspace / ".corsia" / "probe-bin"}
    targets.update({name: Path.home() / relative for name, relative in _TOOLCHAIN_DIRS.items()})
    files: list[Path] = []
    dirs: list[Path] = []
    cases = []
    try:
        for name, directory in targets.items():
            dirs.extend(path for path in (directory, *directory.parents) if not path.exists())
            directory.mkdir(parents=True, exist_ok=True)
            stem = f"corsia-probe-{uuid.uuid4().hex[:8]}"
            script = directory / f"{stem}-script"
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            script.chmod(0o755)
            files.append(script)
            ran = []
            if native is not None:
                command = directory / f"{stem}-native"
                shutil.copy(native, command)
                files.append(command)
                ran.append(("native", _run_raw(name, *_app_envelope(profiles, f"probe-{name}", str(command), []))))
            ran.append(("script", _run_raw(name, *_app_envelope(profiles, f"probe-{name}-s", str(script), []))))
            case = ran[0][1]
            verdicts = [f"{kind} {_exec_verdict(result)}" for kind, result in ran]
            if native is None:
                verdicts.insert(0, "native: no cc")
            case.status, case.detail = "INFO", f"{directory}: " + "; ".join(verdicts)
            cases.append(case)
    finally:
        for path in files:
            path.unlink(missing_ok=True)
        for path in dirs:
            with suppress(OSError):
                path.rmdir()
    brew = next((Path("/opt/homebrew/bin") / name for name in _HOMEBREW_CANDIDATES
                 if (Path("/opt/homebrew/bin") / name).is_file()), None)
    if brew is None:
        cases.append(_Case("T6-homebrew-bin", "INFO", detail="no candidate binary in /opt/homebrew/bin"))
    else:
        case = _run_raw("T6-homebrew-bin", *_app_envelope(profiles, "probe-T6", str(brew), ["--version"]))
        case.status, case.detail = "INFO", f"{brew} --version: {_exec_verdict(case)}"
        cases.append(case)
    return cases


def _write_verdict(case: _Case, target: Path, grants: list[str], must_deny: bool) -> _Case:
    """Record a write probe's result and errno; only the $HOME probe must be denied with EPERM."""
    try:
        probe = json.loads(case.stdout[-1]) if case.stdout else {}
    except ValueError:
        probe = {}
    parent = str(target.parent.resolve())
    # Grants may name /var or /tmp, which resolve under /private, so compare real paths.
    real = [os.path.realpath(grant) for grant in grants]
    inside = [grant for grant in real if parent == grant or parent.startswith(grant.rstrip("/") + "/")]
    exists = target.exists()
    # Keep a timeout or launch failure; the probe's own stdout is already parsed above.
    prior = "" if case.detail.startswith("stdout: ") else f"; {case.detail}" if case.detail else ""
    case.detail = (
        f"{target}: {probe.get('result', 'no report')} code={probe.get('code')} errno={probe.get('errno')} "
        f"exists={exists}; " + (f"INSIDE grant {inside[0]}" if inside else "outside every add-dirs grant") + prior
    )
    if case.status == "PASS":
        denied = probe.get("code") == "EPERM" and not exists and not inside
        case.status = ("PASS" if denied else "FAIL") if must_deny else "INFO"
    return case


def _write_cases(corsia, grants: list[str]) -> list[_Case]:
    """Cases l-n: outside writes from CORSIA's launch, each into a parent created first."""
    suffix = uuid.uuid4().hex[:12]
    tmpdir = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    cases = []
    for name, parent, must_deny in (
        ("l-write-home-outside", Path.home() / f".corsia-mcp-outside-{suffix}", True),
        ("m-write-tmpdir", tmpdir / f"corsia-mcp-outside-{suffix}", False),
        ("n-write-private-tmp", Path("/private/tmp") / f"corsia-mcp-outside-{suffix}", False),
    ):
        target = parent / "probe"
        try:
            parent.mkdir()
        except OSError as error:
            cases.append(_Case(name, "FAIL", detail=f"could not create {parent}: {error}"))
            continue
        try:
            probe = _launch(name, corsia, "node", ["-e", _WRITE_PROBE, str(target)])
            cases.append(_write_verdict(probe, target, grants, must_deny))
        finally:
            shutil.rmtree(parent, ignore_errors=True)
    return cases


def _cache_isolation_cases(workspace: Path, profile_dir: Path, profiles: list[Path]) -> list[_Case]:
    """Prove real Safehouse denies an agent shell and App A access to another app cache."""
    from mcp.client.stdio import get_default_environment
    from tools import shell_guard
    from tools.mcp import _safehouse_stdio_parameters

    def mcp_launch(name: str, target: Path):
        params, profile = _safehouse_stdio_parameters(name, "node", ["-e", _WRITE_PROBE, str(target)], {})
        profiles.append(profile)
        return [params.command, *params.args], {**get_default_environment(), **params.env}, Path(params.cwd), params

    unused = profile_dir / "app-b-cache-target"
    _, _, _, app_b = mcp_launch("cache-app-b", unused)
    app_b_cache = Path(app_b.env["npm_config_cache"]).parent
    app_b_target = app_b_cache / "app-a-must-not-write"
    argv, env, cwd, app_a = mcp_launch("cache-app-a", app_b_target)
    app_a_cache = Path(app_a.env["npm_config_cache"]).parent
    app_case = _write_verdict(_run_raw("r-app-a-write-app-b-cache", argv, env, cwd), app_b_target, [str(app_a_cache)], True)

    agent_target = app_a_cache / "agent-shell-must-not-write"
    command = f"node -e {shlex.quote(_WRITE_PROBE)} {shlex.quote(str(agent_target))}"
    profile = profile_dir / "agent-shell-cache.sb"
    profile.write_text(
        shell_guard.execution_envelope_profile(command, workspace, profile, leased=False), encoding="utf-8",
    )
    agent_case = _write_verdict(
        _run_raw("o-agent-shell-write-app-cache", shell_guard.safehouse_argv(command, workspace, profile),
                 shell_guard.safehouse_subprocess_env(None), workspace),
        agent_target, [str(workspace)], True,
    )
    for target in (app_b_target, agent_target):
        target.unlink(missing_ok=True)
    return [agent_case, app_case]


def _sandbox_denials() -> list[str]:
    """Return at most 20 macOS Sandbox denial operation/path pairs for an npx failure."""
    if sys.platform != "darwin":
        return []
    try:
        result = subprocess.run(
            ["/usr/bin/log", "show", "--last", "2m", "--style", "compact", "--predicate", 'eventMessage CONTAINS "Sandbox:"'],
            capture_output=True, text=True, errors="replace", timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    pairs = []
    for line in result.stdout.splitlines():
        if match := re.search(r"deny\(\d+\)\s+(\S+)\s+(.+)$", line):
            pairs.append(f"{match.group(1)} {match.group(2)}")
    return pairs[-20:]


def _npx_failure_diagnostics(stderr: str, npm_cache: str | None, secret: str) -> list[str]:
    """Preserve full redacted stderr plus bounded app-cache npm logs and Sandbox denials."""
    lines = ["full stderr:", *_tail(stderr, secret, keep=10_000)]
    log_dir = Path(npm_cache or ".") / "_logs"
    for log in sorted(log_dir.glob("*.log"))[-3:]:
        with suppress(OSError):
            lines += [f"npm log {log.name}:", *_tail(log.read_text(encoding="utf-8", errors="replace"), secret, keep=80)]
    return [*lines, "Sandbox denials (operation/path):", *_sandbox_denials()]


def _install_uv(target: Path) -> tuple[_Case, Path | None]:
    """Install uv for this test only (pip --target) and return the bin directory holding uvx."""
    case = _run_raw(
        "f-uvx-install",
        [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--target", str(target), "uv"],
        dict(os.environ), target.parent, timeout=180,
    )
    bin_dir = target / "bin"
    if case.status == "PASS" and not (bin_dir / "uvx").is_file():
        case.status, case.detail = "FAIL", f"pip installed no uvx under {bin_dir}"
    return case, bin_dir if case.status == "PASS" else None


def _crash_reports() -> dict[Path, float]:
    """Map each .ips report in the user and system DiagnosticReports folders to its mtime."""
    found: dict[Path, float] = {}
    for folder in _CRASH_DIRS:
        with suppress(OSError):
            for path in folder.rglob("*.ips"):
                with suppress(OSError):
                    found[path] = path.stat().st_mtime
    return found


def _crash_excerpt(path: Path) -> list[str]:
    """Process, exception and the top faulting-thread frames from one .ips crash report."""
    try:
        header_line, _, body = path.read_text(encoding="utf-8", errors="replace").partition("\n")
        header, report = json.loads(header_line), json.loads(body)
    except (OSError, ValueError) as error:
        return [f"{path}: unreadable ({error})"]
    if not isinstance(report, dict) or "exception" not in report:
        return [f"{path}: bug_type {header.get('bug_type')}, no exception"]
    exception, images = report["exception"], report.get("usedImages", [])
    index, threads = report.get("faultingThread", 0), report.get("threads", [])
    frames = threads[index].get("frames", []) if isinstance(index, int) and index < len(threads) else []
    lines = [
        str(path),
        f"  process: {report.get('procName')} [{report.get('pid')}] {report.get('procPath')} "
        f"(parent {report.get('parentProc')}, responsible {report.get('responsibleProc')})",
        f"  exception: {exception.get('type')} {exception.get('signal')} {exception.get('subtype', '')}",
        f"  termination: {(report.get('termination') or {}).get('indicator')}",
        *(f"  vm region: {line}" for line in str(report.get("vmRegionInfo", "")).splitlines()[:3]),
        f"  faulting thread {index}, top {min(len(frames), 15)} frames:",
    ]
    for number, frame in enumerate(frames[:15]):
        image_index = frame.get("imageIndex")
        image = (
            images[image_index].get("name") or images[image_index].get("path", "?")
            if isinstance(image_index, int) and image_index < len(images) else "?"
        )
        where = (
            f"{frame['symbol']} + {frame.get('symbolLocation', 0)}" if frame.get("symbol")
            else f"0x{frame.get('imageOffset', 0):x}"
        )
        lines.append(f"    {number:2} {image}  {where}")
    return lines


def _new_crashes(before: dict[Path, float], expected: int) -> list[str]:
    """Excerpt the reports written since `before`, waiting up to 30s for ReportCrash to write them."""
    deadline = time.monotonic() + (30 if expected else 0)
    while True:
        new = sorted(path for path, mtime in _crash_reports().items() if before.get(path) != mtime)
        if len(new) >= expected or time.monotonic() >= deadline:
            break
        time.sleep(2)
    lines = [f"new .ips reports: {len(new)} (signalled cases: {expected}; searched {', '.join(map(str, _CRASH_DIRS))})"]
    for path in new[:10]:
        lines += _crash_excerpt(path)
    return lines


def _raw_cases(workspace: Path, policy: Path, profiles: list[Path]) -> tuple[str, list[_Case]]:
    """Cases a-c and g-n: Node under Safehouse defaults and CORSIA's launch, its bisect, npx's routes, outside writes."""
    from mcp.client.stdio import get_default_environment

    from tools.mcp import _safehouse_stdio_parameters

    def corsia(command: str, args: list[str]):
        # The argv, environment and cwd the MCP stdio client would launch.
        params, profile = _safehouse_stdio_parameters(f"raw-{command}", command, args, {})
        profiles.append(profile)
        return [params.command, *params.args], {**get_default_environment(), **params.env}, Path(params.cwd)

    safehouse = shutil.which("safehouse") or "safehouse"
    cases = [_run_raw("a-safehouse-defaults", [safehouse, "--", "node", *_NODE_EXIT], dict(os.environ), workspace)]
    cases[0].status = "INFO"
    try:
        argv, env, cwd = corsia("node", _NODE_EXIT)
        control, control_env, _ = corsia("/usr/bin/true", [])
    except Exception as error:
        return "CORSIA argv (b): unavailable", [*cases, _Case("b-corsia-flags", "FAIL", detail=repr(error))]
    grants = [
        path for option in argv[:argv.index("--")] if option.startswith(("--add-dirs=", "--add-dirs-ro="))
        for path in option.split("=", 1)[1].split(":")
    ]
    without_preload = {key: value for key, value in env.items() if key != "NODE_OPTIONS"}
    p = _run_raw("p-process-title-without-preload", argv, without_preload, cwd)
    # Safehouse's shell reports the crash as 139, which _signal_of names "SIGSEGV via shell".
    p.status = "PASS" if _signal_of(p.returncode).split()[0] == "SIGSEGV" else "FAIL"
    p.detail = f"expected SIGSEGV without preload; {p.detail}"
    q = _run_raw("q-process-title-with-preload", argv, env, cwd)
    diagnostics = [
        _run_raw("b-control-usr-bin-true", control, control_env, cwd),
        *_bisect_cases(argv, env, cwd, policy),
        *_npx_cases(corsia),
    ]
    for case in diagnostics:
        case.status = "INFO"
    return f"CORSIA argv (q): {argv}", [
        *cases,
        p,
        q,
        *diagnostics,
        *_write_cases(corsia, grants),
    ]


def _confinement(
    report: dict, secret: str, blocked: Path, expected_cwd: Path, launched_argvs: list[list[str]],
) -> tuple[str, str]:
    """Require real MCP apps to retain the secret only in env and get EPERM outside grants."""
    write = report.get("write")
    if not isinstance(write, dict):
        write = {}
    errno_is_eperm = write.get("errno") in {-1, errno.EPERM}
    launch_secret = any(secret in json.dumps(argv) for argv in launched_argvs)
    problems = [message for failed, message in (
        (report.get("hasSecret") is not True, "secret env missing"),
        (secret in json.dumps(report.get("argv")), "server argv contains secret"),
        (launch_secret, "launched process argv contains secret"),
        (report.get("cwd") != str(expected_cwd), "launch cwd differs from app-private cwd"),
        (write.get("result") != "denied" or write.get("code") != "EPERM" or not errno_is_eperm, "outside write was not EPERM"),
        (blocked.exists(), "outside target exists"),
    ) if failed]
    summary = (
        f"hasSecret={report.get('hasSecret')} cwd={report.get('cwd')} launchArgvSecret={launch_secret} "
        f"write={write.get('result')} code={write.get('code')} errno={write.get('errno')}"
    )
    return ("FAIL" if problems else "PASS"), "; ".join([summary, *problems])


async def _run_mcp(
    name: str, command: str, args: list[str], timeout: int, *, secret: str, blocked_root: Path, launch: dict, workspace: Path,
) -> _Case:
    """Save and start one local app through the real MCP launch, then inspect it."""
    from infra.settings import get_mcp_servers

    blocked = blocked_root / "probe"
    config = {"command": command, "args": args, "connect_timeout": timeout,
              "env": {"SERVICE_TOKEN": secret, "CORSIA_MCP_DENIED_WRITE": str(blocked)}}
    await asyncio.to_thread(mcp_import.save_import, {name: config})
    case, started = _Case(name), time.monotonic()
    with open(launch["dir"] / f"{name}.stderr", "w+", encoding="utf-8", errors="replace") as errlog:
        launch.update(errlog=errlog, processes=[], argvs=[], envs=[])
        try:
            async with AsyncExitStack() as stack:
                registry = ToolRegistry()
                errors = await connect_mcp_servers({name: get_mcp_servers()[name]}, registry, stack)
                if launch["processes"]:
                    case.returncode = launch["processes"][0].returncode
                if errors:
                    case.status, case.detail = "FAIL", errors[name]
                else:
                    report = json.loads(await registry.get(f"mcp_{name}_inspect").execute())
                    expected_cwd = Path(launch["envs"][0]["npm_config_cache"]).parent / "cwd"
                    case.status, case.detail = _confinement(report, secret, blocked, expected_cwd, launch["argvs"])
        except Exception as error:
            case.status, case.detail = "FAIL", f"{type(error).__name__}: {error}"
        errlog.seek(0)
        stderr = errlog.read()
        launch_secret = any(secret in json.dumps(argv) for argv in launch["argvs"])
        if launch_secret:
            case.status = "FAIL"
            case.detail = "; ".join(filter(None, [case.detail, "launched process argv contains secret"]))
        elif launch["argvs"]:
            case.detail = "; ".join(filter(None, [case.detail, "launchArgvSecret=False"]))
        npm_cache = launch["envs"][0].get("npm_config_cache") if launch["envs"] else None
        case.stderr = _npx_failure_diagnostics(stderr, npm_cache, secret) if name.startswith("e-npx") and case.status == "FAIL" else _tail(stderr, secret)
    case.seconds = time.monotonic() - started
    return case


def _render(lines: list[str], cases: list[_Case], crashes: list[str]) -> str:
    """One row per case (exit, signal, seconds, detail), each non-empty stderr tail, then crash reports."""
    width = max(len(case.name) for case in cases)
    rows = [f"{'case':<{width}}  status  {'rc':>4}  {'signal':<17}  {'secs':>5}  detail"]
    rows += [
        f"{case.name:<{width}}  {case.status:<6}  {'-' if case.returncode is None else case.returncode:>4}  "
        f"{_signal_of(case.returncode):<17}  {case.seconds:5.1f}  {case.detail}"
        for case in cases
    ]
    tails = [
        f"--- {case.name} stderr (last {len(case.stderr)} lines) ---\n" + "\n".join(case.stderr)
        for case in cases if case.stderr
    ]
    return "\n".join([
        "", "=== Safehouse MCP launch cases ===", *lines, "", *rows, "", *tails,
        "", "=== Crash reports written during this test ===", *crashes,
    ])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_local_mcp_launch_cases_under_safehouse(capsys, isolated_db, monkeypatch, store, tmp_path):
    """L3 diagnostic: run every Safehouse launch case, print one table, then fail if any failed.

    Each case starts on its own and keeps its exit, signal and stderr, so one
    failure cannot hide the others under the job's pytest -x.
    """
    from infra.config import WORKSPACE
    from mcp.client import stdio

    host = await asyncio.to_thread(_host_binaries)
    crash_baseline = await asyncio.to_thread(_crash_reports)
    workspace = Path(WORKSPACE)
    _write_npx_fixture(workspace)
    planted = workspace / "node_modules" / "corsia-npx-mcp" / "bin" / "server.js"
    planted.write_text("#!/usr/bin/env node\nprocess.exit(42);\n", encoding="utf-8")
    secret = "synthetic-local-mcp-secret"
    # Each real app below receives its own pre-created HOME target.
    launch: dict = {"dir": tmp_path, "errlog": None, "processes": [], "argvs": [], "envs": []}
    create_process = stdio._create_platform_compatible_process

    async def capture_process(*args, **kwargs):
        # Keep the real process launch, including argv, distinct for every MCP case.
        command = kwargs["command"] if "command" in kwargs else args[0]
        process_args = kwargs["args"] if "args" in kwargs else args[1]
        env = kwargs["env"] if "env" in kwargs else (args[2] if len(args) > 2 else {})
        launch["argvs"].append([command, *process_args])
        launch["envs"].append(env)
        kwargs["errlog"] = launch["errlog"]
        process = await create_process(*args, **kwargs)
        launch["processes"].append(process)
        return process

    monkeypatch.setattr(stdio, "_create_platform_compatible_process", capture_process)
    profiles: list[Path] = []
    try:
        argv_line, cases = await asyncio.to_thread(_raw_cases, workspace, tmp_path / "corsia-rendered.sb", profiles)
        uv_case, uv_bin = await asyncio.to_thread(_install_uv, tmp_path / "uv-target")
        cases.append(uv_case)
        if uv_bin is not None:
            # uvx comes first on PATH for this test only; CORSIA passes PATH through to the app.
            monkeypatch.setenv("PATH", f"{uv_bin}{os.pathsep}{os.environ.get('PATH', '')}")
            host.append(f"uvx (test-installed): {shutil.which('uvx')} ({_host_output([str(uv_bin / 'uvx'), '--version'])})")
        cases.extend(await asyncio.to_thread(_npx_probe_cases, profiles))
        cases.extend(await asyncio.to_thread(_toolchain_exec_cases, workspace, tmp_path, profiles))
        node_app = workspace / "direct-node-fixture.js"
        node_app.write_text(_NODE_MCP_SERVER, encoding="utf-8")
        node_app.chmod(node_app.stat().st_mode | stat.S_IXUSR)
        for name, command, args, timeout in (
            ("d-node-direct", "node", [str(node_app)], 30),
            ("e-npx-bare", "npx", ["corsia-npx-mcp"], 30),
            ("e-npx-y", "npx", ["-y", "corsia-npx-mcp"], 30),
            ("e-npx-no-install", "npx", ["--no-install", "corsia-npx-mcp"], 30),
            ("f-uvx", "uvx", ["--from", str(_write_uvx_fixture(workspace)), "corsia-uvx-mcp"], 90),
        ):
            if command == "npx":
                from infra.config import STATE_ROOT
                from tools.mcp import _mcp_cache_dir

                _write_npx_fixture(_mcp_cache_dir(name, workspace, Path(STATE_ROOT)) / "cwd")
            blocked_root = Path.home() / f".corsia-mcp-outside-{name}-{uuid.uuid4().hex[:12]}"
            blocked_root.mkdir()
            try:
                cases.append(await _run_mcp(
                    name, command, args, timeout, secret=secret, blocked_root=blocked_root,
                    launch=launch, workspace=workspace,
                ))
            finally:
                shutil.rmtree(blocked_root, ignore_errors=True)
        cases.extend(await asyncio.to_thread(_cache_isolation_cases, workspace, tmp_path, profiles))
    finally:
        for profile in profiles:
            profile.unlink(missing_ok=True)

    signalled = sum(_signal_of(case.returncode) not in ("-", "none") for case in cases)
    crashes = await asyncio.to_thread(_new_crashes, crash_baseline, signalled)
    report = _render([*host, argv_line], cases, crashes).replace(secret, "<secret>")
    with capsys.disabled():
        print(report, flush=True)
    if failed := [case.name for case in cases if case.status == "FAIL"]:
        pytest.fail(f"Safehouse launch cases failed: {', '.join(failed)}\n{report}", pytrace=False)
