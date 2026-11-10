"""The lifecycle shell owns release except when launchd teardown is unconfirmed."""

import os
from pathlib import Path

import pytest

from api.restart_shell import PRE_STOP_UPDATE_REFUSED, spawn_restart_lifecycle


@pytest.mark.parametrize(
    ("scenario", "delete_expected"),
    [("refusal", True), ("stop_failure", True), ("bootout_timeout", False),
     ("bootout_unloaded", False), ("killed", False), ("success", False)],
)
def test_update_cleanup_boundary(tmp_path, scenario, delete_expected):
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
        expected = 0 if scenario == "success" else (-9 if scenario == "killed" else (2 if scenario.startswith("bootout_") else PRE_STOP_UPDATE_REFUSED))
        assert child.wait(timeout=5) == expected
    observed = calls.read_text() if calls.exists() else ""
    assert ("DELETE" in observed) is delete_expected
    log = (tmp_path / "lifecycle.log").read_text()
    assert ("cleanup held: launchd teardown unconfirmed" in log) is scenario.startswith("bootout_")


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
    environment = {**os.environ, "PATH": str(tmp_path), "CORSIA_RESTART_BACKEND_PORT": "4111"}
    with (tmp_path / "lifecycle.log").open("w") as log:
        child = spawn_restart_lifecycle(
            script, ["restart-prepared"], environment, "a" * 32, log,
            caller_credential="test-credential",
        )
        assert child.wait(timeout=5) != PRE_STOP_UPDATE_REFUSED
