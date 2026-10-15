"""Local MCP applications use a scope-neutral Safehouse direct-exec launch."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import stat
import subprocess
import time
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


_CASE_TIMEOUT = 15
_NODE_EXIT = ["-e", "process.exit(0)"]
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


def _run_raw(name: str, argv: list[str], env: dict[str, str], cwd: Path, keep: int = 20) -> _Case:
    """Launch one command in its own session and keep its exit and stderr."""
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, errors="replace", start_new_session=True,
        )
    except OSError as error:
        return _Case(name, "FAIL", detail=f"spawn failed: {error}")
    detail = ""
    try:
        _, stderr = process.communicate(timeout=_CASE_TIMEOUT)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        _, stderr = process.communicate()
        detail = f"timed out after {_CASE_TIMEOUT}s"
    status = "PASS" if process.returncode == 0 and not detail else "FAIL"
    return _Case(name, status, process.returncode, time.monotonic() - started, detail, _tail(stderr, keep=keep))


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


def _raw_cases(workspace: Path, policy: Path, profiles: list[Path]) -> tuple[str, list[_Case]]:
    """Cases a-c: a Node exit under Safehouse defaults, under CORSIA's launch, and its bisect."""
    from mcp.client.stdio import get_default_environment

    from tools.mcp import _safehouse_stdio_parameters

    def corsia(command: str, args: list[str]):
        # The argv, environment and cwd the MCP stdio client would launch.
        params, profile = _safehouse_stdio_parameters(command, args, {})
        profiles.append(profile)
        return [params.command, *params.args], {**get_default_environment(), **params.env}, Path(params.cwd)

    safehouse = shutil.which("safehouse") or "safehouse"
    cases = [_run_raw("a-safehouse-defaults", [safehouse, "--", "node", *_NODE_EXIT], dict(os.environ), workspace)]
    try:
        argv, env, cwd = corsia("node", _NODE_EXIT)
        control, control_env, _ = corsia("/usr/bin/true", [])
    except Exception as error:
        return "CORSIA argv (b): unavailable", [*cases, _Case("b-corsia-flags", "FAIL", detail=repr(error))]
    return f"CORSIA argv (b): {argv}", [
        *cases,
        _run_raw("b-corsia-flags", argv, env, cwd),
        _run_raw("b-control-usr-bin-true", control, control_env, cwd),
        *_bisect_cases(argv, env, cwd, policy),
    ]


def _confinement(report: dict, secret: str, blocked: Path) -> tuple[str, str]:
    """Hold a started app to the secret-delivery and outside-write proof."""
    problems = [message for failed, message in (
        (report.get("hasSecret") is not True, "secret env missing"),
        (secret in json.dumps(report.get("argv")), "secret in argv"),
        (report.get("write") != "denied", "outside write not denied"),
        (blocked.exists(), "outside target exists"),
    ) if failed]
    summary = f"hasSecret={report.get('hasSecret')} write={report.get('write')}"
    return ("FAIL" if problems else "PASS"), "; ".join([summary, *problems])


async def _run_mcp(
    name: str, command: str, args: list[str], timeout: int, *, secret: str, blocked_root: Path, launch: dict,
) -> _Case:
    """Save and start one local app through the real MCP launch, then inspect it."""
    from infra.settings import get_mcp_servers

    blocked = blocked_root / f"{name}-must-not-exist"
    config = {"command": command, "args": args, "connect_timeout": timeout,
              "env": {"SERVICE_TOKEN": secret, "CORSIA_MCP_DENIED_WRITE": str(blocked)}}
    await asyncio.to_thread(mcp_import.save_import, {name: config})
    case, started = _Case(name), time.monotonic()
    with open(launch["dir"] / f"{name}.stderr", "w+", encoding="utf-8", errors="replace") as errlog:
        launch.update(errlog=errlog, processes=[])
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
                    case.status, case.detail = _confinement(report, secret, blocked)
        except Exception as error:
            case.status, case.detail = "FAIL", f"{type(error).__name__}: {error}"
        errlog.seek(0)
        case.stderr = _tail(errlog.read(), secret)
    case.seconds = time.monotonic() - started
    return case


def _render(lines: list[str], cases: list[_Case]) -> str:
    """One row per case (exit, signal, seconds, detail), then each non-empty stderr tail."""
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
    return "\n".join(["", "=== Safehouse MCP launch cases ===", *lines, "", *rows, "", *tails])


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
    workspace = Path(WORKSPACE)
    _write_npx_fixture(workspace)
    blocked_root = tmp_path / "outside-safehouse-rw"
    secret = "synthetic-local-mcp-secret"
    # The outside directory exists, so "denied" proves the sandbox rather than a missing parent.
    blocked_root.mkdir()
    launch: dict = {"dir": tmp_path, "errlog": None, "processes": []}
    create_process = stdio._create_platform_compatible_process

    async def capture_process(*args, **kwargs):
        # The real launch, with this case's stderr kept apart from the others.
        kwargs["errlog"] = launch["errlog"]
        process = await create_process(*args, **kwargs)
        launch["processes"].append(process)
        return process

    monkeypatch.setattr(stdio, "_create_platform_compatible_process", capture_process)
    profiles: list[Path] = []
    try:
        argv_line, cases = await asyncio.to_thread(_raw_cases, workspace, tmp_path / "corsia-rendered.sb", profiles)
        node_app = str(workspace / "node_modules" / ".bin" / "corsia-npx-mcp")
        for name, command, args, timeout in (
            ("d-node-direct", "node", [node_app], 30),
            ("e-npx-bare", "npx", ["corsia-npx-mcp"], 30),
            ("e-npx-y", "npx", ["-y", "corsia-npx-mcp"], 30),
            ("e-npx-no-install", "npx", ["--no-install", "corsia-npx-mcp"], 30),
            ("f-uvx", "uvx", ["--from", str(_write_uvx_fixture(workspace)), "corsia-uvx-mcp"], 90),
        ):
            cases.append(await _run_mcp(
                name, command, args, timeout, secret=secret, blocked_root=blocked_root, launch=launch,
            ))
    finally:
        for profile in profiles:
            profile.unlink(missing_ok=True)

    report = _render([*host, argv_line], cases).replace(secret, "<secret>")
    with capsys.disabled():
        print(report, flush=True)
    if failed := [case.name for case in cases if case.status == "FAIL"]:
        pytest.fail(f"Safehouse launch cases failed: {', '.join(failed)}\n{report}", pytrace=False)
