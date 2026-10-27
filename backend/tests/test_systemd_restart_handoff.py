"""The Linux restart owner must acknowledge its private handoff."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from api import restart_shell
from api.restart_shell import spawn_restart_lifecycle


def _prepare(tmp_path: Path, runner_body: str):
    binary = tmp_path / "bin"
    binary.mkdir()
    runner = binary / "systemd-run"
    runner.write_text("#!/bin/bash\n" + runner_body + "\n")
    runner.chmod(0o755)
    script = tmp_path / "corsia"
    script.write_text('#!/bin/bash\necho "restart reached"\n')
    script.chmod(0o755)
    env = {**os.environ, "PATH": f"{binary}:/usr/bin:/bin", "CORSIA_SYSTEMD_SERVICE": "1",
           "CORSIA_RESTART_BACKEND_PORT": "1956"}
    return script, env


def test_transient_owner_acknowledges_and_runs_without_token_in_arguments(tmp_path, monkeypatch):
    monkeypatch.setattr(restart_shell.sys, "platform", "linux")
    script, env = _prepare(tmp_path, 'printf "%s\\n" "$*" > "$TRACE"; exec "${@:5}"')
    env["TRACE"] = str(tmp_path / "argv")
    log = tmp_path / "restart.log"
    with log.open("w") as output:
        process = spawn_restart_lifecycle(script, ["restart"], env, "a" * 32, output,
                                          caller_credential="private-token")
        assert process.wait(timeout=10) == 0
    assert "restart reached" in log.read_text()
    assert "private-token" not in (tmp_path / "argv").read_text()


def test_macos_ignores_the_systemd_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(restart_shell.sys, "platform", "darwin")
    script, env = _prepare(tmp_path, "exit 1")
    with (tmp_path / "restart.log").open("w") as output:
        process = spawn_restart_lifecycle(script, ["restart"], env, "a" * 32, output,
                                          caller_credential="test-value")
        assert process.wait(timeout=10) == 0


def test_unconfirmed_owner_exits_without_starting_restart(tmp_path):
    marker = tmp_path / "started"
    helper = Path(__file__).resolve().parents[1] / "scripts" / "systemd_restart.py"
    process = subprocess.Popen([sys.executable, str(helper)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdin is not None and process.stdout is not None
    command = [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]
    process.stdin.write((json.dumps({"command": command, "environment": dict(os.environ)}) + "\n").encode())
    process.stdin.flush()
    assert process.stdout.readline() == b"ready\n"
    process.stdin.close()
    assert process.wait(timeout=10) == 0
    assert not marker.exists()


def test_failed_transient_start_is_reported_before_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(restart_shell.sys, "platform", "linux")
    script, env = _prepare(tmp_path, "exit 1")
    with (tmp_path / "restart.log").open("w") as output:
        with pytest.raises(OSError):
            spawn_restart_lifecycle(script, ["restart"], env, "a" * 32, output,
                                    caller_credential="private-token")
