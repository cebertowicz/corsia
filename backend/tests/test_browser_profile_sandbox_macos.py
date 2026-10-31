"""Prove the stored-credential hard floor (key store, saved browser profile) in a real macOS shell."""
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
