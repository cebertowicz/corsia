from __future__ import annotations

from pathlib import Path

import pytest

from service_script_fixtures import (  # noqa: F401
    isolated_launch_agents_dir,
    run_bash as _run_bash,
)


def test_lifecycle_child_releases_exact_operation_on_late_nonzero_exit(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "late-exit.log"
    operation_id = "f" * 32
    result = _run_bash(
        """
warn() { :; }
BACKEND_PORT=9999
CORSIA_RESTART_OPERATION_ID="$OPERATION_ID"
source scripts/corsia.d/runtime.sh
current_drain_url() { printf 'owned-drain-url'; }
cancel_drain_if_confirmed() {
    printf 'cancel %s %s\\n' "$1" "$2" >> "$TRACE"
}
late_failure() {
    printf 'child-started\\n' >> "$TRACE"
    return 9
}
run_owned_restart_lifecycle late_failure
""",
        env={"OPERATION_ID": operation_id, "TRACE": str(trace)},
    )

    assert result.returncode == 9
    assert trace.read_text().splitlines() == [
        "child-started",
        f"cancel owned-drain-url {operation_id}",
    ]


def test_manual_lifecycle_cleanup_uses_late_operation_without_hiding_errexit(
    tmp_path: Path,
) -> None:
    """A direct CLI owner releases the operation acquired inside its command."""
    trace = tmp_path / "manual-late-exit.log"
    operation_id = "e" * 32
    result = _run_bash(
        """
set -e
warn() { :; }
BACKEND_PORT=9999
source scripts/corsia.d/runtime.sh
current_drain_url() { printf 'owned-drain-url'; }
cancel_drain_if_confirmed() {
    printf 'cancel %s %s\\n' "$1" "$2" >> "$TRACE"
}
acquire_then_fail() {
    export CORSIA_RESTART_OPERATION_ID="$OPERATION_ID"
    printf 'before-failure\\n' >> "$TRACE"
    false
    printf 'after-failure\\n' >> "$TRACE"
}
run_owned_restart_lifecycle acquire_then_fail
""",
        env={"OPERATION_ID": operation_id, "TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert trace.read_text().splitlines() == [
        "before-failure",
        f"cancel owned-drain-url {operation_id}",
    ]


def test_prebound_predecessor_steering_uses_ready_owner(
    tmp_path: Path,
) -> None:
    """A pre-acquired predecessor operation lets its /ready endpoint decide."""
    trace = tmp_path / "prebound-steering.log"
    clock = tmp_path / "clock"
    clock.write_text("0")
    operation_id = "a" * 32
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
BACKEND_PORT=9999
CORSIA_TOKEN=fixture-token
CORSIA_RESTART_OPERATION_ID="$OPERATION_ID"
source scripts/corsia.d/runtime.sh
runtime_epoch_seconds() {
    now=$(cat "$CLOCK")
    now=$((now + 1))
    printf '%s' "$now" > "$CLOCK"
    printf '%s' "$now"
}
sleep() { :; }
CORSIA_DRAIN_MAX_WAIT=3
curl() {
    printf '%s\n' "$*" >> "$CURL_TRACE"
    case " $* " in
        *" -X POST "*"/api/system/drain/ready?"*)
            printf '{"ok":true,"operation_id":"%s"}\n200' "$OPERATION_ID"
            ;;
        *" -X DELETE "*) return 0 ;;
        *" -X POST "*"/api/system/drain/hard-cap?"*) return 22 ;;
        *)
            printf '{"active":true,"phase":"steering","activeProcesses":0,"operation_id":"%s"}' "$OPERATION_ID"
            ;;
    esac
}
drain_and_wait restart 9999
""",
        env={
            "CLOCK": str(clock),
            "CURL_TRACE": str(trace),
            "OPERATION_ID": operation_id,
        },
    )

    assert result.returncode == 0, result.stderr
    calls = trace.read_text().splitlines()
    assert sum("/api/system/drain/ready?" in call for call in calls) == 1
    assert not any(
        "-X POST" in call
        and "/api/system/drain?" in call
        and "/api/system/drain/ready?" not in call
        for call in calls
    )
    assert not any("-X DELETE" in call for call in calls)


def test_cli_predecessor_steering_uses_ready_owner(
    tmp_path: Path,
) -> None:
    """The old backend's settled steering phase is accepted by its /ready owner."""
    trace = tmp_path / "cli-steering.log"
    clock = tmp_path / "clock"
    clock.write_text("0")
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
BACKEND_PORT=9999
CORSIA_TOKEN=fixture-token
source scripts/corsia.d/runtime.sh
acquire_restart_operation() { return 0; }
runtime_epoch_seconds() {
    now=$(cat "$CLOCK")
    now=$((now + 1))
    printf '%s' "$now" > "$CLOCK"
    printf '%s' "$now"
}
sleep() { :; }
CORSIA_DRAIN_MAX_WAIT=3
curl() {
    printf '%s\n' "$*" >> "$CURL_TRACE"
    case " $* " in
        *" -X POST "*"/api/system/drain/ready?"*)
            printf '{"ok":true,"operation_id":"%s"}\n200' "$operation_id"
            ;;
        *" -X POST "*"/api/system/drain/hard-cap?"*) return 22 ;;
        *" -X POST "*"/api/system/drain?"*)
            printf '{"ok":true,"active":true,"activeProcesses":0,"phase":"steering","operation_id":"%s"}\n200' "$operation_id"
            ;;
        *" -X DELETE "*) return 0 ;;
        *)
            printf '{"active":true,"activeProcesses":0,"phase":"steering","operation_id":"%s"}' "$operation_id"
            ;;
    esac
}
drain_and_wait restart 9999
""",
        env={"CLOCK": str(clock), "CURL_TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    calls = trace.read_text().splitlines()
    assert sum(
        "-X POST" in call
        and "/api/system/drain?" in call
        and "/api/system/drain/ready?" not in call
        for call in calls
    ) == 1
    assert sum("/api/system/drain/ready?" in call for call in calls) == 1
    assert not any("-X DELETE" in call for call in calls)


def test_manual_drain_rechecks_after_transient_ready_conflict(
    tmp_path: Path,
) -> None:
    """A stale zero-work observation keeps its owner and returns to bounded polling."""
    trace = tmp_path / "curl.log"
    ready_attempts = tmp_path / "ready-attempts"
    ready_attempts.write_text("0")
    operation_id = "b" * 32
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
BACKEND_PORT=9999
CORSIA_TOKEN=[REDACTED]
CORSIA_RESTART_OPERATION_ID="$OPERATION_ID"
source scripts/corsia.d/runtime.sh
sleep() { :; }
curl() {
    printf '%s\\n' "$*" >> "$CURL_TRACE"
    case " $* " in
        *" -X POST "*"/api/system/drain/ready?"*)
            count=$(( $(cat "$READY_ATTEMPTS") + 1 ))
            printf '%s' "$count" > "$READY_ATTEMPTS"
            if [ "$count" -eq 1 ]; then
                printf '%s\n409' '{"detail":"restart operation is not quiescent"}'
                return 0
            fi
            printf '{"ok":true,"operation_id":"%s"}\n200' "$OPERATION_ID"
            ;;
        *" -X DELETE "*) return 0 ;;
        *)
            phase=steering
            if [ "$(cat "$READY_ATTEMPTS")" -gt 0 ]; then
                phase=draining
            fi
            printf '{"active":true,"activeProcesses":0,"phase":"%s","operation_id":"%s"}' "$phase" "$OPERATION_ID"
            ;;
    esac
}
drain_and_wait restart 9999
""",
        env={
            "CURL_TRACE": str(trace),
            "OPERATION_ID": operation_id,
            "READY_ATTEMPTS": str(ready_attempts),
        },
    )

    assert result.returncode == 0, result.stderr
    calls = trace.read_text().splitlines()
    assert ready_attempts.read_text() == "2"
    assert sum("/api/system/drain/ready?" in call for call in calls) == 2
    assert sum(
        "-X " not in call and "/api/system/drain?" in call for call in calls
    ) == 2
    assert not any("-X DELETE" in call for call in calls)


def test_manual_drain_cancels_same_body_from_non_409_http_failure(
    tmp_path: Path,
) -> None:
    """The quiescence detail does not mask a different HTTP status."""
    trace = tmp_path / "curl.log"
    operation_id = "c" * 32
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
BACKEND_PORT=9999
CORSIA_TOKEN=[REDACTED]
CORSIA_RESTART_OPERATION_ID="$OPERATION_ID"
source scripts/corsia.d/runtime.sh
curl() {
    printf '%s\\n' "$*" >> "$CURL_TRACE"
    case " $* " in
        *" -X POST "*"/api/system/drain/ready?"*)
            printf '%s\n500' '{"detail":"restart operation is not quiescent"}'
            return 0
            ;;
        *" -X DELETE "*) return 0 ;;
        *)
            printf '{"active":true,"activeProcesses":0,"phase":"draining","operation_id":"%s"}' "$OPERATION_ID"
            ;;
    esac
}
drain_and_wait restart 9999
""",
        env={"CURL_TRACE": str(trace), "OPERATION_ID": operation_id},
    )

    assert result.returncode != 0
    calls = trace.read_text().splitlines()
    assert sum("/api/system/drain/ready?" in call for call in calls) == 1
    assert sum("-X DELETE" in call for call in calls) == 1


@pytest.mark.parametrize(
    ("ready_output", "ready_status"),
    [
        ("", "28"),
        ('{"ok":true}\nnot-a-status', "0"),
        ("not-json\n200", "0"),
        ('{"detail":"restart operation changed"}\n409', "0"),
    ],
)
def test_manual_drain_cancels_ready_transport_or_malformed_response(
    tmp_path: Path,
    ready_output: str,
    ready_status: str,
) -> None:
    """Transport and malformed /ready results remain terminal exact failures."""
    trace = tmp_path / "curl.log"
    operation_id = "d" * 32
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
BACKEND_PORT=9999
CORSIA_TOKEN=[REDACTED]
CORSIA_RESTART_OPERATION_ID="$OPERATION_ID"
source scripts/corsia.d/runtime.sh
curl() {
    printf '%s\n' "$*" >> "$CURL_TRACE"
    case " $* " in
        *" -X POST "*"/api/system/drain/ready?"*)
            printf '%s' "$READY_OUTPUT"
            return "$READY_STATUS"
            ;;
        *" -X DELETE "*) return 0 ;;
        *)
            printf '{"active":true,"phase":"steering","activeProcesses":0,"operation_id":"%s"}' "$OPERATION_ID"
            ;;
    esac
}
drain_and_wait restart 9999
""",
        env={
            "CURL_TRACE": str(trace),
            "OPERATION_ID": operation_id,
            "READY_OUTPUT": ready_output,
            "READY_STATUS": ready_status,
        },
    )

    assert result.returncode != 0
    calls = trace.read_text().splitlines()
    assert sum("/api/system/drain/ready?" in call for call in calls) == 1
    assert sum("-X DELETE" in call for call in calls) == 1


def test_drain_poll_accepts_the_real_operation_status_payload(
    tmp_path: Path,
    isolated_db,
) -> None:
    """The shell's poll parser reads the payload the operation route really returns."""
    import json

    from automation import self_restart
    from infra.drain import get_drain_operation_status

    operation_id = self_restart.try_acquire(None, None, None, None, "update", kind="update")
    assert operation_id is not None
    payload = json.dumps(get_drain_operation_status(operation_id))
    trace = tmp_path / "curl.log"
    result = _run_bash(
        """
warn() { printf 'warn %s\\n' "$*" >&2; }
ok() { :; }
BACKEND_PORT=9999
CORSIA_TOKEN=[REDACTED]
CORSIA_RESTART_OPERATION_ID="$OPERATION_ID"
source scripts/corsia.d/runtime.sh
sleep() { :; }
curl() {
    printf '%s\\n' "$*" >> "$CURL_TRACE"
    case " $* " in
        *" -X POST "*"/api/system/drain/ready?"*)
            printf '{"ok":true,"operation_id":"%s"}\\n200' "$OPERATION_ID"
            ;;
        *" -X DELETE "*) return 0 ;;
        *) printf '%s' "$PAYLOAD" ;;
    esac
}
drain_and_wait restart 9999
""",
        env={"CURL_TRACE": str(trace), "OPERATION_ID": operation_id, "PAYLOAD": payload},
    )

    assert result.returncode == 0, result.stderr
    calls = trace.read_text().splitlines()
    assert sum("/api/system/drain/ready?" in call for call in calls) == 1
    assert not any("-X DELETE" in call for call in calls)

    assert self_restart.abort_operation(operation_id, error="stopped", failure_code="build_failed")
    settled = get_drain_operation_status(operation_id)
    assert settled["active"] is False
    assert settled["activeProcesses"] == 0


def test_forced_service_stop_uses_the_same_bounded_launchd_owner(
    tmp_path: Path,
    isolated_launch_agents_dir: Path,
) -> None:
    trace = tmp_path / "launchctl.log"
    (isolated_launch_agents_dir / "com.corsia.prod.plist").write_text(
        "installed plist\n"
    )
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
BACKEND_PORT=9999
FORCE=true
source scripts/corsia.d/service.sh
require_owned_service_definition() { return 0; }
# The installed file above is a sentinel, not a real plist: this test owns the
# bounded launchd teardown, and identity is proved at its own seam.
service_installed() { return 0; }
service_is_loaded() { return 0; }
service_domain() { printf 'gui/501'; }
service_label() { printf 'com.corsia.prod'; }
drain_and_wait() { printf 'unexpected drain\\n' >> "$LAUNCHCTL_TRACE"; }
launchctl_bootout_wait() {
    printf 'bootout --wait %s bound=%s\\n' "$1" "$SERVICE_PID_EXIT_WAIT_SECONDS" \
        >> "$LAUNCHCTL_TRACE"
}
service_stop
""",
        env={
            "LAUNCH_AGENTS_DIR": str(isolated_launch_agents_dir),
            "LAUNCHCTL_TRACE": str(trace),
        },
    )

    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == [
        "bootout --wait gui/501/com.corsia.prod bound=125",
    ]
