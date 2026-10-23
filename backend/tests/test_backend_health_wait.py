"""The startup wait prints plain words and a usable next step on timeout."""

from __future__ import annotations

from service_script_fixtures import run_bash

_PREAMBLE = """
fail() { printf '%s\\n' "$*"; }
BACKEND_PORT=9999
BACKEND_LOG=/tmp/state/backend.log
source scripts/corsia.d/runtime.sh
T=0
runtime_epoch_seconds() { echo $((1000 + T)); }
sleep() { T=$((T + 1)); }
"""


def test_timeout_names_the_wait_and_one_next_step() -> None:
    result = run_bash(
        _PREAMBLE
        + """
is_backend_running() { return 1; }
CORSIA_HEALTH_WAIT_SECONDS=60
wait_for_backend_health
echo "rc=$?"
""",
    )

    assert result.stdout.splitlines() == [
        "  Starting CORSIA…",
        "  Starting CORSIA…",
        "CORSIA didn't finish starting within 60 seconds.",
        "     Run 'corsia logs' to see why (log: /tmp/state/backend.log).",
        "rc=1",
    ]
    assert "startup phase" not in result.stdout


def test_success_prints_the_start_line_once() -> None:
    result = run_bash(
        _PREAMBLE
        + """
is_backend_running() { [ "$T" -ge 5 ]; }
wait_for_backend_health
echo "rc=$?"
""",
    )

    assert result.stdout.splitlines() == ["  Starting CORSIA…", "rc=0"]


# A launchd-managed service with pid 4242 that rewrites its startup-progress
# file on each tick of the fake clock while ``publishing`` says so.
_SERVICE = """
is_backend_running() { [ -n "${HEALTHY_AT:-}" ] && [ "$T" -ge "$HEALTHY_AT" ]; }
service_is_loaded() { return 0; }
SERVICE_PID=4242
service_loaded_pid() { echo "$SERVICE_PID"; }
kill() { return 0; }
SEQ=0
PHASE=migrating
publishing() { return 0; }
publish() {
    mkdir -p "$CORSIA_STATE_DIR/runtime"
    SEQ=$((SEQ + 1))
    printf '{"schema": 1, "pid": %s, "seq": %s, "phase": "%s", "done": 1, "total": 3}\\n' \\
        "$SERVICE_PID" "$SEQ" "$PHASE" > "$CORSIA_STATE_DIR/runtime/startup-progress.json"
}
sleep() { T=$((T + 1)); tick; if publishing; then publish; fi; }
tick() { :; }
"""


def _wait(tmp_path, body: str):
    return run_bash(
        _PREAMBLE + _SERVICE + body + "\nwait_for_backend_health\necho \"rc=$?\"\n",
        env={"CORSIA_STATE_DIR": str(tmp_path)},
    )


def test_progress_extends_the_wait_past_a_minute(tmp_path) -> None:
    result = _wait(tmp_path, "HEALTHY_AT=200")

    assert result.stdout.splitlines()[-1] == "rc=0", result.stdout
    assert "  CORSIA is upgrading its data; this can take several minutes on a large database…" in result.stdout


def test_a_minute_without_progress_still_fails(tmp_path) -> None:
    result = _wait(tmp_path, 'HEALTHY_AT=500\npublishing() { [ "$T" -lt 20 ]; }')

    lines = result.stdout.splitlines()
    assert lines[-1] == "rc=1"
    assert "CORSIA stopped making progress while starting: nothing changed for 60 seconds." in lines


def test_a_new_service_pid_fails_at_once(tmp_path) -> None:
    result = _wait(tmp_path, 'HEALTHY_AT=500\ntick() { [ "$T" -ne 5 ] || SERVICE_PID=5151; }')

    lines = result.stdout.splitlines()
    assert lines[-1] == "rc=1"
    assert "CORSIA stopped while starting, and launchd started it again." in lines


def test_an_exited_service_fails_at_once(tmp_path) -> None:
    result = _wait(
        tmp_path,
        'HEALTHY_AT=500\ntick() { [ "$T" -ne 5 ] || SERVICE_PID=""; }\n'
        'kill() { [ -n "$SERVICE_PID" ]; }',
    )

    lines = result.stdout.splitlines()
    assert lines[-1] == "rc=1"
    assert "CORSIA stopped while starting." in lines


def test_a_reported_failure_fails_at_once(tmp_path) -> None:
    result = _wait(tmp_path, 'HEALTHY_AT=500\ntick() { [ "$T" -ne 5 ] || PHASE=failed; }')

    lines = result.stdout.splitlines()
    assert lines[-1] == "rc=1"
    assert "CORSIA reported that it could not start." in lines


def test_the_ceiling_ends_a_start_that_keeps_progressing(tmp_path) -> None:
    result = _wait(tmp_path, "HEALTHY_AT=5000\nCORSIA_STARTUP_CEILING_SECONDS=120")

    lines = result.stdout.splitlines()
    assert lines[-1] == "rc=1"
    assert "CORSIA was still starting after 120 seconds, the most one start may take." in lines


def test_another_process_progress_is_not_progress(tmp_path) -> None:
    result = _wait(tmp_path, "HEALTHY_AT=500\nservice_loaded_pid() { echo 777; }")

    lines = result.stdout.splitlines()
    assert lines[-1] == "rc=1"
    assert "CORSIA didn't finish starting within 60 seconds." in lines
