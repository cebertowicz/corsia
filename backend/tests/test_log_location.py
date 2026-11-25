"""The same state root names one log folder across the launcher and backend."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from infra.log_files import logs_dir


VECTOR = Path(__file__).parent / "fixtures" / "log-folder-vector.json"


def test_shared_vector_and_non_darwin_path(tmp_path):
    vector = json.loads(VECTOR.read_text())
    state = Path(vector["state_root"])
    assert logs_dir(state, home=tmp_path, platform="darwin").name == vector["folder"]
    assert logs_dir(state, home=tmp_path, platform="linux") == state / "logs"
    other = tmp_path / "other"
    assert logs_dir(other, home=tmp_path, platform="darwin") != logs_dir(state, home=tmp_path, platform="darwin")


def test_shell_calls_same_python_helper(tmp_path):
    vector = json.loads(VECTOR.read_text())
    backend = Path(__file__).resolve().parents[1]
    script = 'import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from infra.log_files import logs_dir; print(logs_dir(Path(sys.argv[2]), home=Path(sys.argv[3]), platform="darwin").name)'
    result = subprocess.run([sys.executable, "-I", "-c", script, str(backend), vector["state_root"], str(tmp_path)], check=True, capture_output=True, text=True)
    assert result.stdout.strip() == vector["folder"]


def test_symlinked_state_has_one_log_folder(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(state, target_is_directory=True)
    assert logs_dir(alias, home=tmp_path, platform="darwin") == logs_dir(state, home=tmp_path, platform="darwin")
    vector = json.loads(VECTOR.read_text())
    root_alias = tmp_path / "root-alias"
    root_alias.symlink_to(vector["state_root"], target_is_directory=True)
    assert logs_dir(root_alias, home=tmp_path, platform="darwin").name == vector["folder"]


def test_lifecycle_results_stay_in_state_while_restart_logs_move(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from api import restart_shell
    from api.system_admin_helpers import restart_log_path
    from automation import restart_startup, self_restart
    from infra import config

    state_logs = tmp_path / "state" / "logs"
    public_logs = logs_dir(tmp_path / "state", home=tmp_path / "home", platform="darwin")
    public_logs.mkdir(parents=True)
    monkeypatch.setattr(config, "STATE_LOGS_DIR", state_logs)
    monkeypatch.setattr(config, "LOGS_DIR", public_logs)
    assert restart_log_path("restart.log") == public_logs / "restart.log"
    assert restart_log_path("self-restart.log") == public_logs / "self-restart.log"
    operation = "a" * 32
    monkeypatch.setattr(restart_shell.subprocess, "Popen", Mock(return_value=Mock()))
    executable = tmp_path / "corsia"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    with (public_logs / "update.log").open("w") as log:
        process = restart_shell.spawn_restart_lifecycle(executable, ["restart"], {"CORSIA_RESTART_BACKEND_PORT": "1234"}, operation, log, caller_credential="test")
    assert process.lifecycle_result_path == state_logs / f"lifecycle-{operation}.result"
    process.lifecycle_result_path.write_text("done")
    public_result = public_logs / f"lifecycle-{operation}.result"
    public_result.write_text("leave alone")
    monkeypatch.setattr(self_restart, "operation_by_id", lambda _: {"status": "complete"})
    restart_startup._remove_terminal_lifecycle_results(Mock())
    assert not process.lifecycle_result_path.exists()
    assert public_result.exists()


@pytest.mark.parametrize("name,mode", [("restart.log", "w"), ("self-restart.log", "w"), ("update-123.log", "a"), ("install-local-setup.log", "a"), ("service.log", "rb")])
def test_log_opener_refuses_symlink(tmp_path, name, mode):
    from infra.log_files import nofollow_log_opener

    target = tmp_path / "target"
    target.write_text("unchanged")
    link = tmp_path / name
    link.symlink_to(target)
    with pytest.raises(OSError):
        with open(link, mode, opener=nofollow_log_opener):
            pass
    assert target.read_text() == "unchanged"


def test_failure_tail_reads_update_log_in_new_folder(tmp_path, monkeypatch):
    from infra import config
    from infra.update_operation import _failure_log_tail

    folder = logs_dir(tmp_path / "state", home=tmp_path / "home", platform="darwin")
    folder.mkdir(parents=True)
    path = folder / "update-123.log"
    path.write_text("failure details")
    monkeypatch.setattr(config, "LOGS_DIR", folder)
    assert _failure_log_tail({"status": "failed", "failure_detail": json.dumps({"log_path": str(path)})}) == "failure details"
