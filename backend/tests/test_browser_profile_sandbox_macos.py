"""Prove the stored-credential hard floor (key store, saved browser profile, API token) in a real macOS shell."""
import os
import subprocess

import pytest

from host_access import browser_launch
from tools.host_path_policy import ProtectedHostPathPolicy


def _run_in(sandbox: str, command: str, path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", sandbox, "/bin/sh", "-c", command, "sh", str(path)],
        capture_output=True, text=True, check=False,
    )


def _skip_without_nested_sandbox(result: subprocess.CompletedProcess) -> None:
    if result.returncode == 71 and "sandbox_apply: Operation not permitted" in result.stderr:
        if os.environ.get("CORSIA_REQUIRE_SAFEHOUSE") == "1":
            pytest.fail("required sandbox could not be applied")
        pytest.skip("nested macOS sandbox unavailable in this execution environment")


def test_leased_floor_keeps_the_folders_above_each_credential_in_place(tmp_path):
    home, state_root = tmp_path / "home", tmp_path / "state"
    home.mkdir()
    key = state_root / "keys" / "fixture"
    cookie = state_root / browser_launch.PROFILE_ROOT_NAME / "synthetic-cookie"
    for secret in (key, cookie):
        secret.parent.mkdir(parents=True)
        secret.write_text("fixture")
    policy = ProtectedHostPathPolicy(home=home, state_root=state_root)
    sandbox = "(version 1)(allow default)" + policy.hard_floor_sandbox_rules()
    allowed = _run_in(sandbox, 'printf ok > "$1/new" && cat "$1/new"', state_root)
    _skip_without_nested_sandbox(allowed)
    assert allowed.returncode == 0 and allowed.stdout == "ok"
    for secret in (key, cookie):
        for folder in (state_root, tmp_path):
            moved = _run_in(sandbox, f'mv "$1" "$1-x" && cat "$1-x/{secret.relative_to(folder)}"', folder)
            assert moved.returncode != 0 and "fixture" not in moved.stdout, folder
            assert secret.read_text() == "fixture"


def test_profile_hard_floor_denies_shell_reads_and_writes_through_symlinks(tmp_path):
    state_root = tmp_path / "state"
    profile = state_root / browser_launch.PROFILE_ROOT_NAME
    profile.mkdir(parents=True)
    secret = profile / "synthetic-cookie"
    secret.write_text("fixture")
    link = tmp_path / "profile-link"
    link.symlink_to(profile, target_is_directory=True)
    policy = ProtectedHostPathPolicy(home=tmp_path / "home", state_root=state_root)
    sandbox = "(version 1)(allow default)" + policy.hard_floor_sandbox_rules()
    control = tmp_path / "control"
    control.write_text("fixture")
    allowed = _run_in(sandbox, 'cat "$1"', control)
    _skip_without_nested_sandbox(allowed)
    assert allowed.returncode == 0 and allowed.stdout == "fixture"
    for path in (secret, link / secret.name):
        for command in ('cat "$1" >/dev/null', 'printf denied > "$1"'):
            assert _run_in(sandbox, command, path).returncode != 0, (path, command)
    assert secret.read_text() == "fixture"


def test_the_token_folder_stays_closed_under_a_lease(tmp_path):
    home = tmp_path / "home"
    folder = home / ".corsia" / "credentials"
    folder.mkdir(parents=True)
    token = folder / "api-token"
    token.write_text("fixture")
    outside = tmp_path / "outside"
    outside.write_text("other")
    policy = ProtectedHostPathPolicy(home=home, state_root=tmp_path / "state")
    sandbox = "(version 1)(allow default)" + policy.hard_floor_sandbox_rules()
    allowed = _run_in(sandbox, 'printf ok > "$1/.corsia/config.json" && printf ok > "$1/new" && cat "$1/new"', home)
    _skip_without_nested_sandbox(allowed)
    assert allowed.returncode == 0 and allowed.stdout == "ok"
    for command in (
        'cat "$1/api-token"',
        'printf other > "$1/api-token"',
        'printf other > "$1/api-token.tmp-1"',
        f'mv "{outside}" "$1/api-token"',
        'rm "$1/api-token"',
        'ls "$1"',
    ):
        result = _run_in(sandbox, command, folder)
        assert result.returncode != 0 and "fixture" not in result.stdout, command
    assert sorted(path.name for path in folder.iterdir()) == ["api-token"]
    assert token.read_text() == "fixture" and outside.read_text() == "other"


def test_a_linked_corsia_folder_stays_in_place_under_a_lease(tmp_path):
    home, target, other = tmp_path / "home", tmp_path / "elsewhere" / "corsia", tmp_path / "other"
    (target / "credentials").mkdir(parents=True)
    (target / "credentials" / "api-token").write_text("fixture")
    other.mkdir()
    home.mkdir()
    link = home / ".corsia"
    link.symlink_to(target, target_is_directory=True)
    policy = ProtectedHostPathPolicy(home=home, state_root=tmp_path / "state")
    sandbox = "(version 1)(allow default)" + policy.hard_floor_sandbox_rules()
    allowed = _run_in(sandbox, 'printf ok > "$1/.corsia/config.json" && printf ok > "$1/new" && cat "$1/new"', home)
    _skip_without_nested_sandbox(allowed)
    assert allowed.returncode == 0 and allowed.stdout == "ok"
    assert (target / "config.json").read_text() == "ok"
    for command in (
        'rm "$1"',
        'mv "$1" "$1-x"',
        f'ln -sfn "{other}" "$1"',
        f'mv "{target}" "{target}-x"',
    ):
        assert _run_in(sandbox, command, link).returncode != 0, command
        assert link.is_symlink() and os.readlink(link) == str(target), command
        assert (target / "credentials" / "api-token").read_text() == "fixture"
