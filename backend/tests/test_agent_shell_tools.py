"""An agent's shell keeps the person's tools first and finds CORSIA's Python, Node, uv and gh after them."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import shutil
import tempfile
from unittest.mock import AsyncMock

import pytest

from core.turn_core import TurnIdentity
import tools.agent_shell_tools as agent_shell_tools
from tools import managed_runtime, shell as shell_module
from tools.managed_runtime import ManagedRuntimeDownloadError
from tools.shell_environment import safehouse_subprocess_env, shell_env_pass_names

_MANAGED_COMMANDS = managed_runtime.COMMAND_RUNTIME


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    # A fresh Mac has none of these; a runner or developer machine may, which must not change what is fetched.
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, mode=os.F_OK, path=None: (
        None if path is None and name in _MANAGED_COMMANDS else real_which(name, mode, path)))
    monkeypatch.setattr(agent_shell_tools, "_prefetch", None, raising=False)
    return tmp_path


def _runtime_dir(kind: str, home: Path) -> str:
    return str(managed_runtime.runtime_bin_dir(kind, root=home / "Library" / "Caches" / "com.cebertowicz.corsia" / "runtimes"))


def _pin_dirs(home: Path) -> list[str]:
    return [_runtime_dir(kind, home) for kind in ("node", "uv", "gh")]


def _bundle(home: Path, monkeypatch) -> Path:
    bundle = home / "CORSIA.app"
    python = bundle / "Contents" / "Resources" / "python" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.write_text("")
    monkeypatch.setattr(agent_shell_tools, "app_bundle_root", lambda: bundle)
    return python.parent


def _executable(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_the_persons_path_keeps_its_order_and_the_pinned_runtimes_come_last(home, monkeypatch):
    monkeypatch.setenv("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    parts = safehouse_subprocess_env("thread-1")["PATH"].split(":")
    assert parts == ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", *_pin_dirs(home)]


def test_bundled_python_sits_before_usr_bin_so_the_persons_python_wins_and_the_clt_stub_loses(home, monkeypatch):
    python_dir = _bundle(home, monkeypatch)
    monkeypatch.setenv("PATH", "/opt/homebrew/bin:/usr/bin:/bin")
    env = safehouse_subprocess_env("thread-1")
    assert env["PATH"].split(":") == ["/opt/homebrew/bin", str(python_dir), "/usr/bin", "/bin", *_pin_dirs(home)]
    assert not {"UV_PYTHON", "UV_NO_MANAGED_PYTHON"} & set(env)  # a project's own Python choice is left alone
    monkeypatch.setenv("PATH", "/bin")  # no /usr/bin to precede: the app's Python still lands before the pins
    assert safehouse_subprocess_env("thread-1")["PATH"].split(":") == ["/bin", str(python_dir), *_pin_dirs(home)]


def test_a_node_the_person_has_wins_and_on_a_bare_path_the_pin_resolves(home, monkeypatch):
    pin = _executable(Path(_runtime_dir("node", home)), "node")
    theirs = _executable(home / "theirs", "node")
    monkeypatch.setenv("PATH", f"{theirs.parent}:/usr/bin:/bin")
    assert shutil.which("node", path=safehouse_subprocess_env("thread-1")["PATH"]) == str(theirs)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert shutil.which("node", path=safehouse_subprocess_env("thread-1")["PATH"]) == str(pin)


def test_package_caches_are_writable_and_never_the_runtime_folder_or_home(home):
    env = safehouse_subprocess_env("thread-1")
    caches = [Path(env[name]) for name in ("npm_config_cache", "PIP_CACHE_DIR", "UV_CACHE_DIR")]
    runtimes = managed_runtime.runtime_cache_root(home)
    for cache in caches:
        assert not cache.is_relative_to(home) and not cache.is_relative_to(runtimes)
        assert cache.is_relative_to(tempfile.gettempdir())  # a temp root, which the sandbox lets shells write
    assert set(agent_shell_tools.CACHE_ENV_NAMES) <= set(shell_env_pass_names().split(","))
    other = safehouse_subprocess_env("thread-2")
    assert not {env[name] for name in agent_shell_tools.CACHE_ENV_NAMES} & {
        other[name] for name in agent_shell_tools.CACHE_ENV_NAMES
    }


async def _prefetched(monkeypatch, ensure) -> None:
    monkeypatch.setattr(agent_shell_tools, "ensure_runtime", ensure)
    agent_shell_tools.start_prefetch()
    await agent_shell_tools.wait_for_prefetch()


@pytest.mark.asyncio
async def test_the_packaged_app_fetches_each_missing_runtime_once_at_startup(home, monkeypatch):
    _bundle(home, monkeypatch)
    fetched = []
    await _prefetched(monkeypatch, fetched.append)
    assert fetched == ["node", "uv", "gh"]
    agent_shell_tools.start_prefetch()  # a second start never fetches again
    await agent_shell_tools.wait_for_prefetch()
    assert fetched == ["node", "uv", "gh"]


@pytest.mark.asyncio
async def test_a_tool_the_person_already_has_is_never_fetched(home, monkeypatch):
    _bundle(home, monkeypatch)
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, mode=os.F_OK, path=None: (
        "/usr/local/bin/" + name if name in ("node", "npm", "npx") else real_which(name, mode, path)))
    fetched = []
    await _prefetched(monkeypatch, fetched.append)
    assert fetched == ["uv", "gh"]


@pytest.mark.asyncio
async def test_an_installed_runtime_is_not_fetched_again(home, monkeypatch):
    _bundle(home, monkeypatch)
    monkeypatch.setattr(agent_shell_tools, "runtime_fetch_plan", lambda command: None)
    await _prefetched(monkeypatch, lambda kind: pytest.fail("fetched an installed runtime"))


@pytest.mark.asyncio
async def test_a_source_run_fetches_nothing(home, monkeypatch):
    monkeypatch.setattr(agent_shell_tools, "app_bundle_root", lambda: None)
    await _prefetched(monkeypatch, lambda kind: pytest.fail("a source run fetched a runtime"))
    assert agent_shell_tools.prefetch_task() is None


@pytest.mark.asyncio
async def test_a_failed_fetch_is_logged_once_per_runtime_and_shells_still_run(home, monkeypatch, caplog):
    _bundle(home, monkeypatch)
    attempts = []

    def fail(kind):
        attempts.append(kind)
        raise ManagedRuntimeDownloadError("download failed: URLError")

    with caplog.at_level(logging.WARNING, logger=agent_shell_tools.logger.name):
        await _prefetched(monkeypatch, fail)
        await agent_shell_tools.wait_for_prefetch()  # shells after the failure wait for nothing and never retry
    assert attempts == ["node", "uv", "gh"]
    assert sum("could not install" in record.getMessage() for record in caplog.records) == 3


@pytest.mark.asyncio
async def test_bash_waits_for_the_prefetch_before_it_spawns_and_spawns_with_the_tool_path(home, monkeypatch):
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
    order = []

    async def waited():
        order.append("wait")

    async def spawned(*args, **kwargs):
        order.append("spawn")
        raise RuntimeError("stop")

    monkeypatch.setattr(agent_shell_tools, "wait_for_prefetch", waited)
    spawn = AsyncMock(side_effect=spawned)
    monkeypatch.setattr(shell_module.PROCESS_OWNER, "spawn", spawn)
    context = {"_thread_id": "thread-1", "_host_access_turn_identity": TurnIdentity("turn-1", 1)}
    await shell_module.BashTool(working_dir=str(home)).execute("gh --version", **context)
    assert order == ["wait", "spawn"]
    assert spawn.await_args.kwargs["env"]["PATH"].split(":")[-3:] == _pin_dirs(home)


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


def _read_only_grants(home, monkeypatch) -> list[str]:
    argv = _protected_argv(home, monkeypatch)
    assert not any(part.startswith("--add-dirs=") and "CORSIA.app" in part for part in argv)  # never writable
    return next(part for part in argv if part.startswith("--add-dirs-ro=")).split("=", 1)[1].split(":")


def test_a_protected_shell_reads_the_bundled_git_exec_path_and_templates_so_clone_works(home, monkeypatch):
    python_dir = _bundle(home, monkeypatch)
    resources = python_dir.parent.parent
    exec_path = resources / "libexec" / "git-core"
    templates = resources / "share" / "git-core"
    (exec_path / "git-remote-https").parent.mkdir(parents=True)
    (templates / "templates").mkdir(parents=True)
    (resources / "bin").mkdir()  # the PATH's bin folder is not part of Git's runtime and is not granted
    assert _read_only_grants(home, monkeypatch) == [
        str(python_dir.parent.resolve()), str(exec_path.resolve()), str(templates.resolve()),
    ]


def test_each_bundled_git_folder_is_granted_only_when_it_exists(home, monkeypatch):
    python_dir = _bundle(home, monkeypatch)
    exec_path = python_dir.parent.parent / "libexec" / "git-core"
    exec_path.mkdir(parents=True)
    assert _read_only_grants(home, monkeypatch) == [str(python_dir.parent.resolve()), str(exec_path.resolve())]


def test_the_git_folders_are_granted_without_the_bundled_python(home, monkeypatch):
    resources = home / "CORSIA.app" / "Contents" / "Resources"
    (resources / "libexec" / "git-core").mkdir(parents=True)
    monkeypatch.setattr(agent_shell_tools, "app_bundle_root", lambda: home / "CORSIA.app")
    assert _read_only_grants(home, monkeypatch) == [str((resources / "libexec" / "git-core").resolve())]


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


def test_the_build_precompiles_and_immutability_checks_bundled_python():
    build = _repo_file("desktop", "scripts", "build-macos-bundle.sh").read_text()
    test = _repo_file("desktop", "scripts", "test-bundled-python-immutability.sh").read_text()

    assert '"$PYTHON" -m compileall -q -f --invalidation-mode unchecked-hash "$BUNDLE_ROOT"' in build
    assert '"$ROOT/desktop/scripts/test-bundled-python-immutability.sh" "$APP"' in build
    assert "cache_from_source" in test
    assert "env -i PATH=" in test
    for module in ("json", "ssl", "fastapi", "mlx", "core"):
        assert f"import {module}" in test
    assert 'find "$RESOURCES" -type f -name \'*.py\' -exec touch {} +' in test
    assert "stat -f '%m\\t%p\\t%N'" in test
    assert "shasum -a 256" in test
    assert "bundled-python-immutability=passed" in test


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
