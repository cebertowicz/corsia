"""The bootstrap CLI reads the explicitly named store through canonical settings."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from infra.platform_paths import runtime_db_paths


BACKEND = Path(__file__).resolve().parents[1]


def _read_address(state_root):
    env = dict(os.environ)
    # A read of an explicit store must not resolve the home bootstrap at import time.
    env.pop("CORSIA_WORKSPACE", None)
    env.pop("CORSIA_STATE_DIR", None)
    return subprocess.run(
        [sys.executable, "cli.py", "remote-address", "--state-root", str(state_root)],
        cwd=BACKEND, env=env, capture_output=True, text=True, timeout=15,
    )


def test_remote_address_uses_the_named_read_only_store_without_a_workspace(tmp_path):
    state = tmp_path / "state"
    db_path = runtime_db_paths(state)[0]
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO settings VALUES ('devices', ?)",
                   (json.dumps({"enabled": True, "outside_address": "home.example.com:8443"}),))
    result = _read_address(state)
    assert (result.returncode, result.stdout, result.stderr) == (0, "home.example.com:8443\n", "")
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE settings SET value = ?", (json.dumps({"enabled": False, "outside_address": "home.example.com"}),))
    result = _read_address(state)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_remote_address_reports_a_read_failure_without_creating_a_store(tmp_path):
    state = tmp_path / "absent"
    result = _read_address(state)
    assert (result.returncode, result.stdout, result.stderr) == (2, "", "Remote access could not be read.\n")
    assert not state.exists()
