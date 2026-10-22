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


def test_bundled_python_comes_first_and_a_projects_own_python_choice_is_left_alone(home, monkeypatch):
    bundle = home / "CORSIA.app"
    python = bundle / "Contents" / "Resources" / "python" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.write_text("")
    monkeypatch.setattr(agent_shell_tools, "app_bundle_root", lambda: bundle)
    env = safehouse_subprocess_env("thread-1")
    assert env["PATH"].split(":")[0] == str(python.parent)
    assert not {"UV_PYTHON", "UV_NO_MANAGED_PYTHON"} & set(env)


def test_package_caches_are_writable_and_never_the_runtime_folder_or_home(home):
    env = safehouse_subprocess_env("thread-1")
    caches = [Path(env[name]) for name in ("npm_config_cache", "PIP_CACHE_DIR", "UV_CACHE_DIR")]
    runtimes = managed_runtime.runtime_cache_root(home)
    for cache in caches:
        assert not cache.is_relative_to(home) and not cache.is_relative_to(runtimes)
        assert cache.is_relative_to(tempfile.gettempdir())  # a temp root, which the sandbox lets shells write
    assert set(agent_shell_tools.ENV_NAMES) <= set(shell_env_pass_names().split(","))
    other = safehouse_subprocess_env("thread-2")
    assert not {env[name] for name in agent_shell_tools.ENV_NAMES} & {other[name] for name in agent_shell_tools.ENV_NAMES}


@pytest.mark.asyncio
async def test_a_tool_the_person_already_has_is_never_fetched(home, monkeypatch):
    fetch = AsyncMock()
    monkeypatch.setattr(agent_shell_tools, "ensure_runtime", lambda kind: fetch(kind))
    monkeypatch.setattr(agent_shell_tools.shutil, "which", lambda name: "/usr/local/bin/" + name)
    assert await agent_shell_tools.prepare_for_command("node -v && gh --version") is None
    fetch.assert_not_called()


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
    assert message.startswith("Error: CORSIA could not install gh") and "URLError" not in message
    assert "internet" not in message  # the cause is not known, so none is named


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


def _protected_argv(tmp_path, monkeypatch, *, leased: bool = False) -> list[str]:
    from tools import shell_guard

    safehouse = tmp_path / "safehouse"
    safehouse.write_text("")
    monkeypatch.setattr(shell_guard.shutil, "which", lambda _name: str(safehouse))
    monkeypatch.setattr(shell_guard, "get_sandbox_add_dirs_rw", lambda _thread_id, policies=None: [tmp_path / "rw"])
    monkeypatch.setattr(shell_guard, "get_allowed_dirs_ro", lambda policies=None: [])
    monkeypatch.setattr(shell_guard, "agent_account_ro_dirs", lambda _view: [])
    return shell_guard.safehouse_argv("true", tmp_path, tmp_path / "p.sb", leased=leased)


def test_a_protected_shell_reads_the_python_and_runtime_folders_it_has_on_path(home, monkeypatch):
    bundle = home / "CORSIA.app"
    python = bundle / "Contents" / "Resources" / "python" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.write_text("")
    monkeypatch.setattr(agent_shell_tools, "app_bundle_root", lambda: bundle)
    runtimes = managed_runtime.runtime_cache_root()
    runtimes.mkdir(parents=True)
    argv = _protected_argv(home, monkeypatch)
    read_only = next(part for part in argv if part.startswith("--add-dirs-ro=")).split("=", 1)[1].split(":")
    assert read_only == [str(python.parent.parent.resolve()), str(runtimes.resolve())]
    assert not any(part.startswith("--add-dirs=") and str(runtimes.resolve()) in part for part in argv)  # never writable


def test_a_protected_shell_grants_neither_folder_when_it_is_absent(home, monkeypatch):
    monkeypatch.setattr(agent_shell_tools, "app_bundle_root", lambda: None)
    assert not any(part.startswith("--add-dirs-ro=") for part in _protected_argv(home, monkeypatch))


def _repo_file(*parts: str) -> Path:
    return Path(__file__).resolve().parents[2].joinpath(*parts)


def test_the_build_ships_the_pinned_safehouse_the_shell_guard_accepts():
    import json
    import re
    from tools import shell_guard

    script = _repo_file("desktop", "scripts", "build-macos-bundle.sh").read_text()
    version = re.search(r'^readonly SAFEHOUSE_VERSION="([^"]+)"', script, re.MULTILINE).group(1)
    assert tuple(map(int, version.split("."))) >= shell_guard._MIN_SAFEHOUSE_VERSION
    for name in ("SAFEHOUSE_SHA256", "SAFEHOUSE_LICENSE_SHA256"):
        assert re.search(rf'^readonly {name}="[0-9a-f]{{64}}"$', script, re.MULTILINE)
    assert '"$SAFEHOUSE_URL" "$download" "$SAFEHOUSE_SHA256"' in script and "\nfetch_safehouse\n" in script
    assert 'install -m 0755 "$download" "$BUNDLE_ROOT/bin/safehouse"' in script
    assert '"$BUNDLE_ROOT/licenses/safehouse/LICENSE"' in script
    resources = json.loads(_repo_file("desktop", "src-tauri", "tauri.conf.json").read_text())["bundle"]["resources"]
    assert resources["../bundle/bin/safehouse"] == "bin/safehouse"
    smoke = _repo_file("desktop", "scripts", "smoke-macos-bundle.sh").read_text()
    assert '"$RESOURCES/bin/safehouse" --version' in smoke and "-m pip --version" in smoke and "--add-dirs-ro=" in smoke


def test_the_build_refuses_a_download_whose_checksum_is_wrong(tmp_path):
    import hashlib
    import re
    import subprocess

    script = _repo_file("desktop", "scripts", "build-macos-bundle.sh").read_text()
    function = re.search(r"^fetch_verified\(\) \{.*?^\}$", script, re.MULTILINE | re.DOTALL).group(0)
    fail = re.search(r"^fail\(\) .*$", script, re.MULTILINE).group(0)
    source = tmp_path / "safehouse.sh"
    source.write_text("#!/usr/bin/env bash\n")
    good = hashlib.sha256(source.read_bytes()).hexdigest()

    def run(digest: str) -> subprocess.CompletedProcess:
        url = source.as_uri()
        return subprocess.run(
            ["/bin/bash", "-c", f'set -e\n{fail}\n{function}\nfetch_verified "{url}" "{tmp_path}/out" {digest}'],
            capture_output=True, text=True,
        )

    assert run(good).returncode == 0
    wrong = run("0" * 64)
    assert wrong.returncode == 1 and "SHA-256 mismatch" in wrong.stderr
