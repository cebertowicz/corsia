"""Lifecycle result records preserve the launchd teardown boundary."""

import os
from pathlib import Path

import pytest

from api.restart_shell import spawn_restart_lifecycle


@pytest.mark.parametrize(
    "scenario",
    ["refusal", "stop_failure", "bootout_timeout", "bootout_unloaded", "killed", "success"],
)
def test_update_cleanup_boundary(tmp_path, scenario):
    service = Path(__file__).resolve().parents[2] / "scripts/corsia.d/service.sh"
    script = tmp_path / "corsia"
    script.write_text(f'''#!/bin/bash
source "{service}"
fail() {{ :; }}
launchctl_bootout_wait() {{
    case "$SCENARIO" in bootout_timeout) return 124 ;; success) return 0 ;; *) return 1 ;; esac
}}
service_is_loaded() {{ [ "$SCENARIO" = stop_failure ]; }}
if [ "$SCENARIO" = refusal ]; then return 1; fi
if [ "$SCENARIO" = killed ]; then kill -KILL "$$"; fi
bootout_service_and_wait domain label || return $?
return 0
''')
    script.chmod(0o755)
    curl = tmp_path / "curl"
    curl.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$CALLS"\n')
    curl.chmod(0o755)
    calls = tmp_path / "calls"
    environment = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
                   "CORSIA_RESTART_BACKEND_PORT": "4111", "SCENARIO": scenario,
                   "CALLS": str(calls)}
    with (tmp_path / "lifecycle.log").open("w") as log:
        child = spawn_restart_lifecycle(script, ["restart-prepared"], environment,
                                        "a" * 32, log, caller_credential="test-credential")
        expected = 0 if scenario == "success" else (-9 if scenario == "killed" else (2 if scenario.startswith("bootout_") else 1))
        assert child.wait(timeout=5) == expected
    observed = calls.read_text() if calls.exists() else ""
    assert "DELETE" not in observed
    record = child.lifecycle_result_path
    assert record.exists() is (scenario not in {"success", "killed"})
    if record.exists():
        assert ("unconfirmed_bootout=true" in record.read_text()) is scenario.startswith("bootout_")


@pytest.mark.parametrize("body", [
    "_corsia_update_stopping=1; exit 79",
    "_corsia_unconfirmed_bootout=true; exit 79",
    "kill -KILL $$",
])
def test_no_pre_stop_signal_after_stop_or_unknown_exit(tmp_path, body):
    script = tmp_path / "corsia"
    script.write_text(f"#!/bin/bash\n{body}\n")
    script.chmod(0o755)
    curl = tmp_path / "curl"
    curl.write_text("#!/bin/bash\nexit 52\n")
    curl.chmod(0o755)
    environment = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "CORSIA_RESTART_BACKEND_PORT": "4111"}
    with (tmp_path / "lifecycle.log").open("w") as log:
        child = spawn_restart_lifecycle(
            script, ["restart-prepared"], environment, "a" * 32, log,
            caller_credential="test-credential",
        )
        assert child.wait(timeout=5) != 0
    if body == "kill -KILL $$":
        assert not child.lifecycle_result_path.exists()
    else:
        record = child.lifecycle_result_path.read_text()
        assert "stopping=true" in record or "unconfirmed_bootout=true" in record
