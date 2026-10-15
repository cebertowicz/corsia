"""Each agent thread's Google Workspace home: placed outside the workspace, granted to that shell alone."""
from __future__ import annotations

import stat
from pathlib import Path

import pytest

from connections import gws_agent_home
from tools import shell_environment, shell_guard

THREAD_A = "11111111-aaaa-4aaa-8aaa-111111111111"
THREAD_B = "22222222-bbbb-4bbb-8bbb-222222222222"


@pytest.fixture
def homes_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "gws-homes"
    monkeypatch.setenv(gws_agent_home.ROOT_ENV, str(root))
    return root


def _argv(monkeypatch, tmp_path: Path, gws_home: Path | None) -> list[str]:
    safehouse = tmp_path / "safehouse"
    safehouse.write_text("")
    monkeypatch.setattr(shell_guard.shutil, "which", lambda _name: str(safehouse))
    monkeypatch.setattr(shell_guard, "get_sandbox_add_dirs_rw", lambda _thread_id, policies=None: [tmp_path / "rw"])
    monkeypatch.setattr(shell_guard, "get_allowed_dirs_ro", lambda policies=None: [])
    return shell_guard.safehouse_argv("true", tmp_path, tmp_path / "p.sb", gws_home=gws_home)


def _rw_dirs(argv: list[str]) -> list[str]:
    return [dirs for part in argv if part.startswith("--add-dirs=") for dirs in part.split("=", 1)[1].split(":")]


def test_home_is_created_private_under_the_root_per_thread(homes_root):
    home = gws_agent_home.home_for(THREAD_A)

    assert home == homes_root / THREAD_A
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert gws_agent_home.home_for(THREAD_B) == homes_root / THREAD_B


def test_existing_home_path_does_not_create_a_credential_location(homes_root):
    home = gws_agent_home.existing_home_for(THREAD_A)

    assert home == homes_root / THREAD_A
    assert not home.exists()


def test_home_inside_the_workspace_is_refused(monkeypatch):
    from infra.config import WORKSPACE

    monkeypatch.setenv(gws_agent_home.ROOT_ENV, str(WORKSPACE / "gws-homes"))

    assert gws_agent_home.home_for(THREAD_A) is None
    assert not (WORKSPACE / "gws-homes").exists()


@pytest.mark.parametrize("thread_id", [None, "", "../escape", "a/b", ".hidden", "x" * 65])
def test_a_thread_id_that_is_not_a_safe_name_gets_no_home(homes_root, thread_id):
    assert gws_agent_home.home_for(thread_id) is None
    assert not homes_root.exists()


def test_homes_are_off_in_test_mode_without_an_explicit_root(monkeypatch):
    monkeypatch.delenv(gws_agent_home.ROOT_ENV, raising=False)
    monkeypatch.setenv("CORSIA_TEST_MODE", "1")

    assert gws_agent_home.home_for(THREAD_A) is None


def test_owner_argv_grants_its_home_read_write_and_another_thread_gets_neither(homes_root, monkeypatch, tmp_path):
    home_a = gws_agent_home.home_for(THREAD_A)
    home_b = gws_agent_home.home_for(THREAD_B)

    argv_a = _argv(monkeypatch, tmp_path, home_a)
    argv_b = _argv(monkeypatch, tmp_path, home_b)
    argv_none = _argv(monkeypatch, tmp_path, None)

    assert str(home_a.resolve()) in _rw_dirs(argv_a)
    assert str(home_b.resolve()) not in _rw_dirs(argv_a)
    assert str(home_b.resolve()) in _rw_dirs(argv_b)
    assert not any(str(home_a.resolve()) in part for part in argv_b)
    assert not any(str(root) in part for part in argv_none for root in (home_a.resolve(), home_b.resolve()))
    assert not any(part.startswith("--add-dirs-ro=") and str(homes_root.resolve()) in part for part in argv_a)


def test_leased_shell_is_unchanged(homes_root, monkeypatch, tmp_path):
    home = gws_agent_home.home_for(THREAD_A)
    safehouse = tmp_path / "safehouse"
    safehouse.write_text("")
    monkeypatch.setattr(shell_guard.shutil, "which", lambda _name: str(safehouse))

    argv = shell_guard.safehouse_argv("true", tmp_path, tmp_path / "p.sb", leased=True, gws_home=home)

    assert not any(str(home.resolve()) in part for part in argv)


def test_shell_env_names_only_the_threads_own_home(homes_root):
    env_a: dict[str, str] = {}
    env_b: dict[str, str] = {}

    home_a = shell_environment.bind_gws_home(env_a, THREAD_A)
    shell_environment.bind_gws_home(env_b, THREAD_B)

    assert env_a == {gws_agent_home.SHELL_ENV: str(home_a)}
    assert env_b == {gws_agent_home.SHELL_ENV: str(homes_root / THREAD_B)}
    assert gws_agent_home.SHELL_ENV in shell_environment.shell_env_pass_names().split(",")


def test_no_home_means_no_env_name_so_the_helper_fails_closed(monkeypatch):
    monkeypatch.delenv(gws_agent_home.ROOT_ENV, raising=False)
    monkeypatch.setenv("CORSIA_TEST_MODE", "1")
    env: dict[str, str] = {}

    assert shell_environment.bind_gws_home(env, THREAD_A) is None
    assert env == {}


def test_an_inherited_home_name_never_reaches_a_shell(monkeypatch):
    monkeypatch.setenv(gws_agent_home.SHELL_ENV, "/tmp/someone-elses-home")

    env = shell_environment.safehouse_subprocess_env(THREAD_A)

    assert gws_agent_home.SHELL_ENV not in env



def test_a_spawns_context_names_and_grants_only_its_own_threads_home(homes_root, monkeypatch, tmp_path):
    _, _, _, env = shell_environment.shell_execution_context({"_thread_id": THREAD_A}, export_invocation=False)
    argv = _argv(monkeypatch, tmp_path, shell_environment.own_gws_home(env))

    assert env[gws_agent_home.SHELL_ENV] == str(homes_root / THREAD_A)
    assert str((homes_root / THREAD_A).resolve()) in _rw_dirs(argv)
    assert not any(THREAD_B in part for part in argv)
