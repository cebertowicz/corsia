"""Update log timestamps do not leak into displayed output."""

import json
import re
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException

from api import settings_restart
from api.restart_shell import LIFECYCLE_SHELL_COMMAND
from infra import config
from infra.update_operation import _failure_log_tail

TIMESTAMP = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"


def test_prepare_records_start_and_end(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    script = tmp_path / "corsia"
    script.write_text("#!/bin/bash\ntest -s \"$TEST_LOG\" || exit 1\nprintf 'prepared\\n'\n")
    script.chmod(0o755)
    settings_restart._prepare_current_release(script, {
        "CORSIA_RESTART_OPERATION_ID": "test", "TEST_LOG": str(tmp_path / "update-test.log"),
    })
    lines = (tmp_path / "update-test.log").read_text().splitlines()
    assert re.fullmatch(TIMESTAMP + r" Prepare started", lines[0])
    assert lines[1] == "prepared"
    assert re.fullmatch(TIMESTAMP + r" Prepare ended in \d+s \(exit 0\)", lines[2])


@pytest.mark.parametrize("exit_code", [0, 41])
def test_unwritable_log_preserves_prepare_outcome(tmp_path, monkeypatch, exit_code):
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    target = tmp_path / "target"
    target.write_text("unchanged")
    (tmp_path / "update-test.log").symlink_to(target)
    script = tmp_path / "corsia"
    script.write_text(f"#!/bin/bash\nexit {exit_code}\n")
    script.chmod(0o755)
    env = {"CORSIA_RESTART_OPERATION_ID": "test"}
    if exit_code:
        with pytest.raises(HTTPException) as error:
            settings_restart._prepare_current_release(script, env)
        assert error.value.status_code == 500
        assert error.value.update_failure_code == "fetch_failed"
    else:
        settings_restart._prepare_current_release(script, env)
    assert target.read_text() == "unchanged"


def test_lifecycle_line_is_timed_only_in_envelope(tmp_path, monkeypatch):
    runtime = Path(__file__).resolve().parents[2] / "scripts/corsia.d/runtime.sh"
    command = 'source "$1"; lifecycle_say "  Drain started..."'
    plain = subprocess.run(["/bin/bash", "-c", command, "test", str(runtime)], capture_output=True, text=True, check=True)
    script = tmp_path / "corsia"
    script.write_text(f'source "{runtime}"\nlifecycle_say "  Drain started..."\n')
    timed = subprocess.run(
        ["/bin/bash", "-c", LIFECYCLE_SHELL_COMMAND, "test", str(script), "restart-prepared"],
        capture_output=True, text=True, check=True,
    )
    assert plain.stdout == "  Drain started...\n"
    assert re.fullmatch(TIMESTAMP + r"   Drain started\.\.\.\n", timed.stdout)
    path = tmp_path / "update-test.log"
    path.write_text(timed.stdout)
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    operation = {"status": "failed", "failure_detail": json.dumps({"log_path": str(path)})}
    assert _failure_log_tail(operation) == plain.stdout


def test_failure_tail_does_not_show_a_partial_timestamp(tmp_path, monkeypatch):
    path = tmp_path / "update-test.log"
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    operation = {"status": "failed", "failure_detail": json.dumps({"log_path": str(path)})}
    for cut in range(1, 21):
        message = "x" * (6000 + cut - 21)
        path.write_text("2026-01-02T03:04:05Z " + message)
        assert _failure_log_tail(operation) == message
    path.write_text("x" * 6100)
    assert _failure_log_tail(operation) == "x" * 6000
