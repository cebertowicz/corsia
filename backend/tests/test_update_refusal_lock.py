"""A refused packaged update releases its lock even when DELETE fails."""

import os
import sys
from pathlib import Path


def test_refusal_releases_update_lock_before_failed_cleanup(tmp_path, monkeypatch):
    from api.restart_shell import spawn_restart_lifecycle

    root = Path(__file__).resolve().parents[2]
    corsia_text = (root / "scripts/corsia").read_text()
    fail = "fail() {" + corsia_text.split("fail() {", 1)[1].split("\n}", 1)[0] + "\n}"
    stage = tmp_path / "staged"
    stage.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    trace = tmp_path / "trace"
    curl = tmp_path / "curl"
    curl.write_text('''#!/bin/bash
"$PYTHON_FOR_TEST" -c 'import fcntl, os; f = open(os.environ["LOCK_FOR_TEST"], "r+"); fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB); open(os.environ["TRACE_FOR_TEST"], "w").write("released")'
exit 7
''')
    curl.chmod(0o755)
    monkeypatch.setattr("api.restart_shell.shutil.which", lambda *_args, **_kwargs: str(curl))
    script = tmp_path / "corsia"
    script.write_text(f'''#!/bin/bash
source "{root / 'scripts/corsia.d/app_mode.sh'}"
RED=""; NC=""
{fail}
require_owned_service_definition() {{ :; }}
service_is_loaded() {{ return 0; }}
backend_pids() {{ :; }}
is_backend_running() {{ return 1; }}
app_mode_staged_path() {{ printf '%s' "$STAGE_FOR_TEST"; }}
app_mode_previous_path() {{ printf '%s' "$STAGE_FOR_TEST.previous"; }}
PYTHON="$PYTHON_FOR_TEST"
CORSIA_STATE_DIR="$STATE_FOR_TEST"
FORCE=false
app_mode_restart_prepared
''')
    script.chmod(0o755)
    log = tmp_path / "update.log"
    env = {
        **os.environ, "CORSIA_RESTART_BACKEND_PORT": "4111", "PYTHON_FOR_TEST": sys.executable,
        "STATE_FOR_TEST": str(state), "STAGE_FOR_TEST": str(stage),
        "LOCK_FOR_TEST": str(state / "update.lock"), "TRACE_FOR_TEST": str(trace),
    }
    with log.open("w") as output:
        child = spawn_restart_lifecycle(script, ["restart-prepared"], env, "a" * 32, output, caller_credential="test-token")
        assert child.wait(timeout=10) != 0
    assert trace.read_text() == "released"
    assert "current backend is not healthy" in log.read_text()
    assert "cleanup curl exit 7" in log.read_text()
