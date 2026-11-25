"""The macOS log grant must not weaken the protected state floor."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

from infra.log_files import logs_dir
from tools import host_path_policy
from tools.host_path_policy import ProtectedHostPathPolicy


def test_log_grant_and_write_denial_precede_state_floor(tmp_path, monkeypatch):
    monkeypatch.setattr(host_path_policy.sys, "platform", "darwin")
    home = tmp_path / "home"
    logs = home / "Library" / "Logs" / "CORSIA"
    state = logs / "private-state"
    policy = ProtectedHostPathPolicy(home=home, state_root=state)
    rules = policy.sandbox_rules()
    instance = logs_dir(state, home=home, platform="darwin")
    sibling = logs_dir(tmp_path / "other-state", home=home, platform="darwin")
    grant = f'(allow file-read* (subpath "{instance}"))'
    deny = f'(deny file-write* (subpath "{logs}"))'
    floor = f'(deny file-read* file-write* (subpath "{state}"))'
    assert f'(allow file-read-metadata (literal "{logs.parent}"))' in rules
    assert f'(allow file-read-metadata (literal "{logs}"))' in rules
    log_grants = [line for line in rules.splitlines()
                  if line.startswith('(allow file-read* (subpath "' + str(logs))]
    assert log_grants == [grant]
    assert str(sibling) not in rules
    assert rules.index(grant) < rules.index(deny) < rules.index(floor)
    assert policy.ordinary_denial(instance / "service.log", for_write=False) is None
    assert policy.ordinary_denial(instance / "service.log", for_write=True)
    assert policy.ordinary_denial(state / "secret", for_write=False)
    outside = tmp_path / "outside"
    outside.mkdir()
    logs.mkdir(parents=True)
    (logs / "alias").symlink_to(outside, target_is_directory=True)
    assert policy.ordinary_denial(logs / "alias" / "file", for_write=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS-only")
def test_log_rules_against_a_disposable_base_deny(tmp_path):
    home = tmp_path / "home"
    logs = home / "Library" / "Logs"
    corsia = logs / "CORSIA"
    corsia.mkdir(parents=True)
    (logs / "other").mkdir()
    private = corsia / "private-state"
    private.mkdir()
    (private / "secret").write_text("private")
    instance = logs_dir(private, home=home, platform="darwin")
    instance.mkdir()
    log = instance / "service.log"
    log.write_text("disposable log")
    sibling = logs_dir(tmp_path / "other-state", home=home, platform="darwin")
    sibling.mkdir()
    (sibling / "service.log").write_text("sibling log")
    policy = ProtectedHostPathPolicy(home=home, state_root=private)
    profile = f'(version 1)(allow default)(deny file-read* file-write* (subpath "{logs}"))' + policy.sandbox_rules()

    def run(command: str, path: Path):
        return subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", profile, "/bin/sh", "-c", command, "sh", str(path)],
            capture_output=True, text=True, check=False,
        )

    read = run('cat "$1"', log)
    if read.returncode == 71 and "sandbox_apply: Operation not permitted" in read.stderr:
        if os.environ.get("CORSIA_REQUIRE_SAFEHOUSE") == "1":
            pytest.fail("required nested sandbox could not be applied")
        pytest.skip("nested macOS sandbox unavailable")
    assert read.returncode == 0 and read.stdout == "disposable log"
    assert run('cat "$1"', sibling / "service.log").returncode != 0
    assert run('printf x > "$1"', corsia / "new").returncode != 0
    assert run('ln -s /tmp "$1"', corsia / "link").returncode != 0
    assert run('cat "$1"', private / "secret").returncode != 0
    assert run('ls "$1"', logs / "other").returncode != 0
