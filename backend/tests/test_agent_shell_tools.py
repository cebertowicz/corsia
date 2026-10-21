"""An agent's shell finds CORSIA's Python, Node, uv and gh first, fetching a missing one on first use."""
from __future__ import annotations

from pathlib import Path
import tempfile
from unittest.mock import AsyncMock

import pytest

from core.turn_core import TurnIdentity
import tools.agent_shell_tools as agent_shell_tools
from tools import managed_runtime, shell as shell_module
from tools.managed_runtime import ManagedRuntimeDownloadError
from tools.shell_environment import safehouse_subprocess_env, shell_env_pass_names


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    return tmp_path


def _runtime_dir(kind: str, home: Path) -> str:
    return str(managed_runtime.runtime_bin_dir(kind, root=home / "Library" / "Caches" / "com.cebertowicz.corsia" / "runtimes"))


def test_shell_path_leads_with_the_pinned_runtimes_even_before_they_are_fetched(home):
    parts = safehouse_subprocess_env("thread-1")["PATH"].split(":")
    assert parts[:3] == [_runtime_dir("node", home), _runtime_dir("uv", home), _runtime_dir("gh", home)]
    assert parts[3:] == ["/usr/bin", "/bin"]


def test_bundled_python_comes_first_and_uv_uses_it(home, monkeypatch):
    bundle = home / "CORSIA.app"
    python = bundle / "Contents" / "Resources" / "python" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.write_text("")
    monkeypatch.setattr(agent_shell_tools, "app_bundle_root", lambda: bundle)
    env = safehouse_subprocess_env("thread-1")
    assert env["PATH"].split(":")[0] == str(python.parent)
    assert env["UV_PYTHON"] == str(python) and env["UV_NO_MANAGED_PYTHON"] == "1"


def test_package_caches_are_writable_and_never_the_runtime_folder_or_home(home):
    env = safehouse_subprocess_env("thread-1")
    caches = [Path(env[name]) for name in ("npm_config_cache", "PIP_CACHE_DIR", "UV_CACHE_DIR")]
    runtimes = managed_runtime.runtime_cache_root(home)
    for cache in caches:
        assert not cache.is_relative_to(home) and not cache.is_relative_to(runtimes)
        assert cache.is_relative_to(tempfile.gettempdir())  # a temp root, which the sandbox lets shells write
    assert set(agent_shell_tools.ENV_NAMES) <= set(shell_env_pass_names().split(","))


@pytest.mark.asyncio
@pytest.mark.parametrize("command,fetched", [
    ("gh --version", {"gh"}),
    ("cd app && npm install left-pad && FOO=1 npx tsc", {"node"}),
    ("uvx ruff --version | head -1", {"uv"}),
    ("ls; gh api user && node -v", {"gh", "node"}),
    ("echo gh node npm; grep -r node README.md; git commit -m 'use npx'", set()),
])
async def test_first_use_fetches_only_the_runtimes_the_command_runs(home, monkeypatch, command, fetched):
    ensure = []
    monkeypatch.setattr(agent_shell_tools, "ensure_runtime", lambda kind: ensure.append(kind))
    assert await agent_shell_tools.prepare_for_command(command) is None
    assert set(ensure) == fetched


@pytest.mark.asyncio
async def test_an_installed_runtime_is_not_fetched_again(home, monkeypatch):
    monkeypatch.setattr(agent_shell_tools, "runtime_is_installed", lambda kind: True)
    monkeypatch.setattr(agent_shell_tools, "ensure_runtime", lambda kind: pytest.fail("fetched an installed runtime"))
    assert await agent_shell_tools.prepare_for_command("gh --version") is None


@pytest.mark.asyncio
async def test_a_failed_fetch_refuses_the_command_with_the_reason(home, monkeypatch):
    def fail(kind):
        raise ManagedRuntimeDownloadError("download failed: URLError")
    monkeypatch.setattr(agent_shell_tools, "ensure_runtime", fail)
    message = await agent_shell_tools.prepare_for_command("gh --version")
    assert message.startswith("Error: CORSIA could not install gh") and "download failed" in message


@pytest.mark.asyncio
async def test_a_script_that_runs_a_missing_tool_gets_it_fetched_for_the_next_try(home, monkeypatch):
    ensure = []
    monkeypatch.setattr(agent_shell_tools, "ensure_runtime", lambda kind: ensure.append(kind))
    note = await agent_shell_tools.note_after_not_found("./build.sh: line 4: npm: command not found")
    assert ensure == ["node"] and "Run the command again" in note
    assert await agent_shell_tools.note_after_not_found("make: git: command not found") is None


@pytest.mark.asyncio
async def test_bash_fetches_before_it_spawns_and_spawns_with_the_tool_path(home, monkeypatch):
    for name, value in {
        "bind_agent_account": AsyncMock(return_value=None),
        "load_shell_repo_policies": lambda *args: ([], []),
        "is_safehouse_available": lambda: True,
        "safehouse_version_error": lambda: None,
        "_resolve_execution_cwd": AsyncMock(return_value=(home, None)),
        "execution_envelope_for_consumed_action": lambda *args, **kwargs: object(),
        "prepare_execution_envelope": AsyncMock(return_value=(None, ["true"])),
    }.items():
        monkeypatch.setattr(shell_module, name, value)
    monkeypatch.setattr("tools.host_access_gate.consume_current_action_permit", AsyncMock(return_value=None))
    spawn = AsyncMock(side_effect=RuntimeError("stop"))
    monkeypatch.setattr(shell_module.PROCESS_OWNER, "spawn", spawn)
    context = {"_thread_id": "thread-1", "_host_access_turn_identity": TurnIdentity("turn-1", 1)}
    tool = shell_module.BashTool(working_dir=str(home))

    monkeypatch.setattr(agent_shell_tools, "prepare_for_command", AsyncMock(return_value="Error: no network"))
    assert await tool.execute("gh --version", **context) == "Error: no network"
    spawn.assert_not_awaited()

    monkeypatch.setattr(agent_shell_tools, "prepare_for_command", AsyncMock(return_value=None))
    await tool.execute("gh --version", **dict(context))
    assert spawn.await_args.kwargs["env"]["PATH"].split(":")[:3] == [
        _runtime_dir("node", home), _runtime_dir("uv", home), _runtime_dir("gh", home)]
