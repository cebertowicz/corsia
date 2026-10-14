from __future__ import annotations

import json
from pathlib import Path
import plistlib
import re
import stat
import sys

import pytest

from service_script_fixtures import (  # noqa: F401
    isolated_launch_agents_dir,
    run_bash as _run_bash,
)
from infra.default_port import DEFAULT_BACKEND_PORT


_RUNTIME_PREAMBLE = """
warn() { :; }
ok() { :; }
BACKEND_PORT=9999
CORSIA_TOKEN=test-token
source scripts/corsia.d/runtime.sh
acquire_restart_operation() { return 0; }
"""


def test_seed_disposable_account_preserves_mint_failure_detail() -> None:
    result = _run_bash(
        """
source scripts/corsia.d/accounts.sh
named_account_exists() { return 1; }
account_cli() {
    if [ "$1" = user ]; then
        cat > /dev/null
        return 0
    fi
    printf 'mint failed: account tables missing\n' >&2
    return 1
}
seed_disposable_account harness harness
""",
        env={"PYTHON": sys.executable},
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "mint failed: account tables missing\n"


def test_seed_disposable_account_prints_only_the_token_on_success() -> None:
    result = _run_bash(
        """
source scripts/corsia.d/accounts.sh
named_account_exists() { return 1; }
account_cli() {
    if [ "$1" = user ]; then
        cat > /dev/null
        return 0
    fi
    printf 'minted-token\n'
}
seed_disposable_account harness harness
""",
        env={"PYTHON": sys.executable},
    )

    assert result.returncode == 0
    assert result.stdout == "minted-token\n"
    assert result.stderr == ""


def test_unmanaged_backend_does_not_inherit_production_safety_bypasses(
    tmp_path: Path,
) -> None:
    fake_python = tmp_path / "python"
    capture = tmp_path / "backend.env"
    backend = tmp_path / "backend"
    backend.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_nohup = fake_bin / "nohup"
    fake_nohup.write_text("#!/bin/sh\nexec \"$@\"\n")
    fake_nohup.chmod(fake_nohup.stat().st_mode | stat.S_IXUSR)
    fake_python.write_text("#!/bin/sh\nenv > \"$ENV_CAPTURE\"\n")
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)

    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { return 1; }
BACKEND_PORT=9999
BACKEND_DIR="$TEST_BACKEND"
BACKEND_LOG="$TEST_BACKEND/backend.log"
PYTHON="$TEST_PYTHON"
source scripts/corsia.d/runtime.sh
is_backend_running() { return 1; }
backend_pids() { :; }
start_backend || exit
# The fake python writes its environment and exits immediately, so waiting for
# the backgrounded child is deterministic. A fixed poll budget is not: this
# harness previously allowed 0.5s, which a loaded self-hosted runner exceeds.
wait
for _ in 1 2 3 4 5; do
    [ -f "$ENV_CAPTURE" ] && exit 0
    sleep 0.1
done
exit 1
""",
        env={
            "ENV_CAPTURE": str(capture),
            "CORSIA_ALLOW_HOME_WORKSPACE_IN_CI": "1",
            "CORSIA_TEST_MODE": "1",
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "TEST_BACKEND": str(backend),
            "TEST_PYTHON": str(fake_python),
        },
    )

    assert result.returncode == 0, result.stderr
    launched_environment = capture.read_text().splitlines()
    assert "CORSIA_TEST_MODE=1" not in launched_environment
    assert "CORSIA_ALLOW_HOME_WORKSPACE_IN_CI=1" not in launched_environment


def test_drain_does_not_cancel_an_unconfirmed_lost_start_response(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "curl.log"
    result = _run_bash(
        _RUNTIME_PREAMBLE
        + """
curl() {
    printf '%s\\n' "$*" >> "$CURL_TRACE"
    return 22
}
drain_and_wait restart 9999
""",
        env={"CURL_TRACE": str(trace)},
    )

    assert result.returncode != 0
    calls = trace.read_text().splitlines()
    post = next(
        call
        for call in calls
        if "-X POST" in call and "/api/system/drain?" in call
    )
    assert re.search(r'"operation_id":"([0-9a-f]{32})"', post)
    assert not any("-X DELETE" in call for call in calls)


def test_drain_surfaces_bounded_refusal_detail() -> None:
    result = _run_bash(
        _RUNTIME_PREAMBLE
        + """
warn() { printf 'WARN: %s\\n' "$*" >&2; }
curl() {
    case " $* " in
        *" -X POST "*"/api/system/drain?"*)
            for argument in "$@"; do
                [ "$argument" = "-sf" ] && return 22
            done
            printf '{"detail":"could not acquire restart cutoff"}\\n409'
            ;;
        *" -X DELETE "*) return 0 ;;
    esac
}
drain_and_wait restart 9999
""",
    )

    assert result.returncode != 0
    assert "WARN: Could not set drain mode: could not acquire restart cutoff" in result.stderr


@pytest.mark.parametrize(
    "response",
    [
        '{"detail":""}\n409',
        '{"detail":" surrounded by whitespace "}\n409',
        json.dumps({"detail": "not\nprintable"}) + "\n409",
        "not-json\n409",
        json.dumps({"detail": "x" * 201}) + "\n409",
    ],
)
def test_drain_keeps_generic_failure_for_unusable_refusal_detail(response: str) -> None:
    result = _run_bash(
        _RUNTIME_PREAMBLE
        + """
warn() { printf 'WARN: %s\\n' "$*" >&2; }
curl() {
    case " $* " in
        *" -X POST "*"/api/system/drain?"*)
            for argument in "$@"; do
                [ "$argument" = "-sf" ] && return 22
            done
            printf '%s' "$REFUSAL_RESPONSE"
            ;;
        *" -X DELETE "*) return 0 ;;
    esac
}
drain_and_wait restart 9999
""",
        env={"REFUSAL_RESPONSE": response},
    )

    assert result.returncode != 0
    assert result.stderr == "WARN: Could not set drain mode\n"


def test_drain_keeps_generic_failure_for_start_transport_error() -> None:
    result = _run_bash(
        _RUNTIME_PREAMBLE
        + """
warn() { printf 'WARN: %s\\n' "$*" >&2; }
curl() { return 22; }
drain_and_wait restart 9999
""",
    )

    assert result.returncode != 0
    assert result.stderr == "WARN: Could not set drain mode\n"


def test_drain_uses_preacquired_exact_operation_with_legacy_backend(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "curl.log"
    operation = tmp_path / "operation"
    result = _run_bash(
        _RUNTIME_PREAMBLE
        + """
acquire_restart_operation() {
    printf '%s' "$1" > "$OPERATION"
}
curl() {
    printf '%s\\n' "$*" >> "$CURL_TRACE"
    current="$(cat "$OPERATION")"
    case " $* " in
        *" -X POST "*"/api/system/drain/ready?"*)
            printf '{"ok":true,"operation_id":"%s"}\n200' "$current"
            ;;
        *" -X POST "*"/api/system/drain?"*)
            printf '{"ok":true,"active":true,"activeProcesses":0,"operation_id":"%s","reason":"%s"}\n200' "$current" "$reason"
            ;;
        *)
            printf '{"active":true,"activeProcesses":0,"operation_id":"%s"}\n200' "$current"
            ;;
    esac
}
drain_and_wait restart 9999 || exit
printf '%s' "$CORSIA_RESTART_OPERATION_ID" > "$OPERATION"
""",
        env={"CURL_TRACE": str(trace), "OPERATION": str(operation)},
    )

    assert result.returncode == 0, result.stderr
    assert re.fullmatch(r"[0-9a-f]{32}", operation.read_text())
    assert "corsia-operation:" in trace.read_text()


def test_acquire_restart_operation_calls_the_canonical_owner(tmp_path: Path) -> None:
    fake_python = tmp_path / "python"
    trace = tmp_path / "owner-args"
    operation_id = "d" * 32
    fake_python.write_text(
        "#!/bin/bash\n"
        "printf '%s\\n' \"$3\" \"$4\" \"$5\" > \"$OWNER_TRACE\"\n"
        "printf '%s' \"$4\"\n"
    )
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)

    result = _run_bash(
        """
BACKEND_DIR=/owned/backend
PYTHON="$FAKE_PYTHON"
source scripts/corsia.d/runtime.sh
acquire_restart_operation "$OPERATION_ID" "cli restart marker"
""",
        env={
            "FAKE_PYTHON": str(fake_python),
            "OPERATION_ID": operation_id,
            "OWNER_TRACE": str(trace),
        },
    )

    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == [
        "/owned/backend",
        operation_id,
        "cli restart marker",
    ]


def test_drain_refuses_before_post_when_exact_acquisition_loses(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "curl.log"
    result = _run_bash(
        _RUNTIME_PREAMBLE
        + """
acquire_restart_operation() { return 1; }
curl() {
    printf '%s\\n' "$*" >> "$CURL_TRACE"
    case " $* " in
        *" -X POST "*"/api/system/drain?"*)
            printf '{"ok":true,"active":true,"activeProcesses":0,"operation_id":"01234567-89a","reason":"%s"}\n200' "$reason"
            ;;
        *) return 22 ;;
    esac
}
drain_and_wait restart 9999
""",
        env={"CURL_TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert not trace.exists()


def test_prepared_update_adopts_its_legacy_settings_operation(tmp_path: Path) -> None:
    operation = tmp_path / "operation"
    result = _run_bash(
        _RUNTIME_PREAMBLE
        + """
curl() {
    case " $* " in
        *" -X POST "*"/api/system/drain/ready?"*)
            printf '%s\n200' '{"ok":true,"operation_id":"01234567-89a"}'
            ;;
        *" -X POST "*"/api/system/drain?"*)
            printf '%s' '{"ok":true,"active":true,"activeProcesses":0,"operation_id":"01234567-89a","reason":"update"}'
            ;;
        *)
            if [[ " $* " == *" --write-out "* ]]; then
                printf '%s\n200' '{"active":true,"activeProcesses":0,"operation_id":"01234567-89a","reason":"update"}'
            else
                printf '%s' '{"active":true,"activeProcesses":0,"operation_id":"01234567-89a","reason":"update"}'
            fi
            ;;
    esac
}
drain_and_wait "update and restart" 9999 legacy-settings-update || exit
printf '%s' "$CORSIA_RESTART_OPERATION_ID" > "$OPERATION"
""",
        env={"OPERATION": str(operation)},
    )

    assert result.returncode == 0, result.stderr
    assert operation.read_text() == "01234567-89a"


def test_cli_prepared_update_acquires_new_operation_without_legacy_handoff(
    tmp_path: Path,
) -> None:
    operation = tmp_path / "operation"
    result = _run_bash(
        _RUNTIME_PREAMBLE
        + """
curl() {
    case " $* " in
        *" -X POST "*"/api/system/drain/ready?"*)
            current="$(cat "$OPERATION")"
            printf '{"ok":true,"operation_id":"%s"}\n200' "$current"
            ;;
        *" -X POST "*"/api/system/drain?"*)
            body="$*"
            current="${body#*operation_id\\\":\\\"}"
            current="${current%%\\\"*}"
            request_reason="${body#*reason\\\":\\\"}"
            request_reason="${request_reason%%\\\",\\\"operation_id*}"
            printf '%s' "$current" > "$OPERATION"
            printf '{"ok":true,"active":true,"activeProcesses":0,"operation_id":"%s","reason":"%s"}\n200' \
                "$current" "$request_reason"
            ;;
        *)
            if [ -s "$OPERATION" ]; then
                current="$(cat "$OPERATION")"
                if [[ " $* " == *" --write-out "* ]]; then
                    printf '{"active":true,"activeProcesses":0,"operation_id":"%s"}\n200' "$current"
                else
                    printf '{"active":true,"activeProcesses":0,"operation_id":"%s"}' "$current"
                fi
            else
                printf '%s' '{"active":false,"activeProcesses":0}'
            fi
            ;;
    esac
}
drain_and_wait "update and restart" 9999 legacy-settings-update || exit
printf '%s' "$CORSIA_RESTART_OPERATION_ID" > "$OPERATION"
""",
        env={"OPERATION": str(operation)},
    )

    assert result.returncode == 0, result.stderr
    assert re.fullmatch(r"[0-9a-f]{32}", operation.read_text())


def test_prepared_update_refuses_a_competing_legacy_operation(tmp_path: Path) -> None:
    trace = tmp_path / "curl.log"
    result = _run_bash(
        _RUNTIME_PREAMBLE
        + """
curl() {
    printf '%s\n' "$*" >> "$CURL_TRACE"
    printf '%s' '{"active":true,"operation_id":"01234567-89a","reason":"another restart"}'
}
drain_and_wait "update and restart" 9999 legacy-settings-update
""",
        env={"CURL_TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert not any("-X POST" in call for call in trace.read_text().splitlines())


def test_prepared_update_does_not_adopt_after_lost_legacy_status(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "curl.log"
    first_read = tmp_path / "first-read"
    result = _run_bash(
        _RUNTIME_PREAMBLE
        + """
curl() {
    printf '%s\\n' "$*" >> "$CURL_TRACE"
    case " $* " in
        *" -X POST "*"/api/system/drain/ready?"*)
            printf '%s\n200' '{"ok":true,"operation_id":"01234567-89a"}'
            ;;
        *" -X POST "*"/api/system/drain?"*)
            printf '{"ok":true,"active":true,"activeProcesses":0,"operation_id":"01234567-89a","reason":"%s"}\n200' "$reason"
            ;;
        *)
            if [ ! -e "$FIRST_READ" ]; then
                : > "$FIRST_READ"
                return 22
            fi
            printf '%s' '{"active":true,"activeProcesses":0,"operation_id":"01234567-89a"}'
            ;;
    esac
}
drain_and_wait "update and restart" 9999 legacy-settings-update
""",
        env={"CURL_TRACE": str(trace), "FIRST_READ": str(first_read)},
    )

    assert result.returncode != 0
    assert not any("-X POST" in call for call in trace.read_text().splitlines())


def test_prepared_update_cancels_exact_legacy_operation_after_malformed_repost(
    tmp_path: Path,
) -> None:
    cancelled = tmp_path / "cancelled"
    result = _run_bash(
        _RUNTIME_PREAMBLE
        + """
curl() {
    case " $* " in
        *" -X POST "*)
            printf '%s' '{"ok":true,"operation_id":"competing-operation"}'
            ;;
        *)
            printf '%s' '{"active":true,"status":"in_flight","operation_id":"01234567-89a","reason":"update"}'
            ;;
    esac
}
cancel_failed_drain() { printf '%s' "$2" > "$CANCELLED"; }
begin_legacy_settings_drain 'http://127.0.0.1:9999/api/system/drain?token=test'
""",
        env={"CANCELLED": str(cancelled)},
    )

    assert result.returncode != 0
    assert cancelled.read_text() == "01234567-89a"


def test_request_bound_drain_reuses_the_backend_operation(tmp_path: Path) -> None:
    trace = tmp_path / "curl.log"
    result = _run_bash(
        _RUNTIME_PREAMBLE
        + """
CORSIA_RESTART_OPERATION_ID=op-owned
curl() {
    printf '%s\\n' "$*" >> "$CURL_TRACE"
    case " $* " in
        *" -X POST "*"/api/system/drain/ready?"*)
            printf '%s\n200' '{"ok":true,"operation_id":"op-owned"}'
            ;;
        *" -X POST "*"/api/system/drain?"*)
            return 28
            ;;
        *)
            printf '%s\n200' '{"active":true,"activeProcesses":0,"operation_id":"op-owned"}'
            ;;
    esac
}
drain_and_wait restart 9999
""",
        env={"CURL_TRACE": str(trace)},
    )

    calls = trace.read_text().splitlines()
    assert result.returncode == 0, result.stderr
    assert not any("-X POST" in call and "/api/system/drain?" in call for call in calls)
    assert any("/api/system/drain?" in call and "-X POST" not in call for call in calls)
    assert any("/api/system/drain/ready?" in call for call in calls)


def test_request_bound_drain_cancels_only_its_exact_operation(tmp_path: Path) -> None:
    trace = tmp_path / "curl.log"
    result = _run_bash(
        _RUNTIME_PREAMBLE
        + """
CORSIA_RESTART_OPERATION_ID=op-owned
curl() {
    printf '%s\\n' "$*" >> "$CURL_TRACE"
    case " $* " in
        *" -X DELETE "*) return 0 ;;
        *) return 22 ;;
    esac
}
drain_and_wait restart 9999
""",
        env={"CURL_TRACE": str(trace)},
    )

    assert result.returncode != 0
    delete = next(
        call for call in trace.read_text().splitlines() if "-X DELETE" in call
    )
    assert "operation_id=op-owned" in delete


@pytest.mark.parametrize(
    "payload",
    [
        "{}",
        '{"ok": false, "operation_id": "op-start"}',
        '{"ok": true, "operation_id": ""}',
        '{"ok": true, "operation_id": 7}',
        '{"ok": true, "operation_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}',
        "not-json",
    ],
)
def test_drain_cancels_malformed_start_response(
    tmp_path: Path,
    payload: str,
) -> None:
    trace = tmp_path / "curl.log"
    result = _run_bash(
        _RUNTIME_PREAMBLE
        + """
curl() {
    printf '%s\\n' "$*" >> "$CURL_TRACE"
    case " $* " in
        *" -X DELETE "*) return 0 ;;
        *" -X POST "*) printf '%s\n200' "$START_PAYLOAD" ;;
        *) printf '{"operation_id":"%s","status":"in_flight","phase":"steering"}' "$operation_id" ;;
    esac
}
drain_and_wait restart 9999
""",
        env={"CURL_TRACE": str(trace), "START_PAYLOAD": payload},
    )

    assert result.returncode != 0
    calls = trace.read_text().splitlines()
    post = next(
        call
        for call in calls
        if "-X POST" in call and "/api/system/drain?" in call
    )
    match = re.search(r'"operation_id":"([0-9a-f]{32})"', post)
    assert match is not None
    delete = next(call for call in calls if "-X DELETE" in call)
    assert f"operation_id={match.group(1)}" in delete


def test_drain_fails_closed_when_status_poll_fails(tmp_path: Path) -> None:
    trace = tmp_path / "curl.log"
    drain_state = tmp_path / "drain-state"
    result = _run_bash(
        _RUNTIME_PREAMBLE
        + """
curl() {
    printf '%s\\n' "$*" >> "$CURL_TRACE"
    case " $* " in
        *" -X DELETE "*) printf 'inactive' > "$DRAIN_STATE"; return 0 ;;
        *" -X POST "*"/api/system/drain?"*)
            printf 'active' > "$DRAIN_STATE"
            printf '{"ok":true,"active":true,"activeProcesses":1,"operation_id":"%s"}\n200' "$operation_id"
            return 0
            ;;
        *) return 22 ;;
    esac
}
drain_and_wait restart 9999
""",
        env={"CURL_TRACE": str(trace), "DRAIN_STATE": str(drain_state)},
    )

    assert result.returncode != 0
    assert (
        "-sf --max-time 5 -X DELETE "
        "http://127.0.0.1:9999/api/system/drain?token=test-token"
    ) in trace.read_text()
    assert drain_state.read_text() == "inactive"


@pytest.mark.parametrize(
    "payload",
    [
        "{}",
        '{"active": false, "activeProcesses": 0}',
        '{"active": 1, "activeProcesses": 0}',
        '{"activeProcesses": 0}',
        '{"activeProcesses": "0"}',
        '{"activeProcesses": -1}',
        '{"active": true, "activeProcesses": 0, "operation_id": "wrong-op"}',
        "not-json",
    ],
)
def test_drain_fails_closed_when_status_is_malformed(
    tmp_path: Path,
    payload: str,
) -> None:
    trace = tmp_path / "curl.log"
    result = _run_bash(
        _RUNTIME_PREAMBLE
        + """
curl() {
    printf '%s\\n' "$*" >> "$CURL_TRACE"
    case " $* " in
        *" -X DELETE "*) return 0 ;;
        *" -X POST "*"/api/system/drain?"*)
            printf '{"ok":true,"active":true,"activeProcesses":1,"operation_id":"%s"}\n200' "$operation_id"
            ;;
        *) printf '%s\n200' "${STATUS_PAYLOAD/__OPERATION_ID__/$operation_id}" ;;
    esac
}
drain_and_wait restart 9999
""",
        env={"CURL_TRACE": str(trace), "STATUS_PAYLOAD": payload},
    )

    assert result.returncode != 0
    assert "-X DELETE" in trace.read_text()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ('{"active": false, "activeProcesses": 0}', "ended before drain completed"),
        (
            '{"active": true, "activeProcesses": 0, "operation_id": "wrong-op"}',
            "replaced before drain completed",
        ),
    ],
)
def test_drain_names_well_formed_ended_or_replaced_status(
    tmp_path: Path,
    payload: str,
    expected: str,
) -> None:
    trace = tmp_path / "curl.log"
    warnings = tmp_path / "warnings.log"
    result = _run_bash(
        _RUNTIME_PREAMBLE
        + """
warn() { printf '%s\\n' "$*" >> "$WARNING_TRACE"; }
curl() {
    printf '%s\\n' "$*" >> "$CURL_TRACE"
    case " $* " in
        *" -X DELETE "*) return 0 ;;
        *" -X POST "*"/api/system/drain?"*)
            printf '{"ok":true,"active":true,"activeProcesses":1,"operation_id":"%s"}\n200' "$operation_id"
            ;;
        *) printf '%s\n200' "$STATUS_PAYLOAD" ;;
    esac
}
drain_and_wait restart 9999
""",
        env={
            "CURL_TRACE": str(trace),
            "WARNING_TRACE": str(warnings),
            "STATUS_PAYLOAD": payload,
        },
    )

    assert result.returncode != 0
    assert expected in warnings.read_text()
    assert "malformed" not in warnings.read_text().lower()
    assert "-X DELETE" in trace.read_text()


def test_drain_times_out_before_ready_and_forces_hard_cap(tmp_path: Path) -> None:
    trace = tmp_path / "curl.log"
    result = _run_bash(
        _RUNTIME_PREAMBLE
        + """
curl() {
    printf '%s\\n' "$*" >> "$CURL_TRACE"
    case " $* " in
        *" -X POST "*"/api/system/drain/hard-cap?"*)
            printf '{"ok":true,"force_stop_ready":true,"operation_id":"%s"}\n200' "$operation_id"
            ;;
        *" -X POST "*"/api/system/drain?"*)
            printf '{"ok":true,"active":true,"activeProcesses":1,"operation_id":"%s"}\n200' "$operation_id"
            ;;
        *)
            printf '{"active":true,"activeProcesses":1,"operation_id":"%s"}\n200' "$operation_id"
            ;;
    esac
}
sleep() { :; }
CORSIA_DRAIN_MAX_WAIT=2
drain_and_wait restart 9999
""",
        env={"CURL_TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert "-X DELETE" not in trace.read_text()
    assert "api/system/drain/hard-cap?" in trace.read_text()
    assert '"operation_id":"' in trace.read_text()


def test_drain_timeout_uses_elapsed_wall_clock(tmp_path: Path) -> None:
    trace = tmp_path / "curl.log"
    clock = tmp_path / "clock"
    clock.write_text("0")
    result = _run_bash(
        _RUNTIME_PREAMBLE
        + """
runtime_epoch_seconds() {
    local now
    now=$(cat "$CLOCK")
    now=$((now + 4))
    printf '%s' "$now" > "$CLOCK"
    printf '%s' "$now"
}
curl() {
    printf '%s\n' "$*" >> "$CURL_TRACE"
    case " $* " in
        *" -X POST "*"/api/system/drain/hard-cap?"*)
            printf '{"ok":true,"force_stop_ready":true,"operation_id":"%s"}\n200' "$operation_id"
            ;;
        *" -X POST "*"/api/system/drain?"*)
            printf '{"ok":true,"active":true,"activeProcesses":1,"operation_id":"%s"}\n200' "$operation_id"
            ;;
        *)
            printf '{"active":true,"activeProcesses":1,"operation_id":"%s"}\n200' "$operation_id"
            ;;
    esac
}
sleep() { :; }
CORSIA_DRAIN_MAX_WAIT=6
drain_and_wait restart 9999
""",
        env={"CLOCK": str(clock), "CURL_TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    status_polls = [
        line for line in trace.read_text().splitlines()
        if "/api/system/drain?" in line and " -X " not in f" {line} "
    ]
    assert len(status_polls) == 1


def test_drain_poll_names_the_exact_blocking_turn(tmp_path: Path) -> None:
    trace = tmp_path / "curl.log"
    result = _run_bash(
        _RUNTIME_PREAMBLE
        + """
curl() {
    printf '%s\n' "$*" >> "$CURL_TRACE"
    case " $* " in
        *" -X POST "*"/api/system/drain/hard-cap?"*)
            printf '{"ok":true,"force_stop_ready":true,"operation_id":"%s"}\n200' "$operation_id"
            ;;
        *" -X POST "*"/api/system/drain?"*)
            printf '{"ok":true,"active":true,"activeProcesses":1,"operation_id":"%s"}\n200' "$operation_id"
            ;;
        *)
            printf '{"active":true,"activeProcesses":5,"operation_id":"%s","activeTurns":2,"ownedProcessGroups":1,"browserCommands":2,"blockingTurns":[{"threadId":"thread-815f581e","title":"Inspect Batch 2 history/tool storage"},{"threadId":"thread-other","title":"Other turn"}]}\n200' "$operation_id"
            ;;
    esac
}
sleep() { :; }
CORSIA_DRAIN_MAX_WAIT=2
drain_and_wait restart 9999
""",
        env={"CURL_TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert (
        "Inspect Batch 2 history/tool storage (thread-815f581e)"
        in result.stdout
    )
    assert "1 more active turn" in result.stdout
    assert "1 owned process group" in result.stdout
    assert "2 browser commands" in result.stdout


def test_drain_poll_treats_blocker_text_as_data(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    payload = json.dumps({
        "active": True,
        "activeProcesses": 1,
        "operation_id": "__OPERATION_ID__",
        "activeTurns": 1,
        "blockingTurns": [{
            "threadId": "thread-hostile",
            "title": (
                f"$(touch {marker}) `touch {marker}.backtick`"
            ),
        }],
    })
    result = _run_bash(
        _RUNTIME_PREAMBLE
        + """
curl() {
    case " $* " in
        *" -X POST "*"/api/system/drain/hard-cap?"*)
            printf '{"ok":true,"force_stop_ready":true,"operation_id":"%s"}\n200' "$operation_id"
            ;;
        *" -X POST "*"/api/system/drain?"*)
            printf '{"ok":true,"active":true,"activeProcesses":1,"operation_id":"%s"}\n200' "$operation_id"
            ;;
        *) printf '%s\n200' "${STATUS_PAYLOAD/__OPERATION_ID__/$operation_id}" ;;
    esac
}
sleep() { :; }
CORSIA_DRAIN_MAX_WAIT=2
drain_and_wait restart 9999
""",
        env={"STATUS_PAYLOAD": payload},
    )

    assert result.returncode == 0, result.stderr
    assert "$(touch" in result.stdout
    assert not marker.exists()
    assert not Path(f"{marker}.backtick").exists()


def test_drain_reports_poll_and_cancel_failures_without_masking_either() -> None:
    result = _run_bash(
        """
warn() { printf 'WARN: %s\\n' "$*" >&2; }
ok() { :; }
BACKEND_PORT=9999
CORSIA_TOKEN=test-token
source scripts/corsia.d/runtime.sh
acquire_restart_operation() { return 0; }
curl() {
    case " $* " in
        *" -X POST "*"/api/system/drain?"*)
            printf '{"ok":true,"active":true,"activeProcesses":1,"operation_id":"%s"}\n200' "$operation_id"
            return 0
            ;;
        *) return 22 ;;
    esac
}
drain_and_wait restart 9999
"""
    )

    assert result.returncode != 0
    assert "WARN: Could not read drain status" in result.stderr
    assert "WARN: Could not cancel drain mode after failed drain" in result.stderr


def test_drain_marks_ready_only_when_zero_active_processes(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "curl.log"
    result = _run_bash(
        _RUNTIME_PREAMBLE
        + """
curl() {
    printf '%s\\n' "$*" >> "$CURL_TRACE"
    case " $* " in
        *" -X POST "*"/api/system/drain/ready?"*)
            printf '{"ok":true,"operation_id":"%s","phase":"restarting"}\n200' "$operation_id"
            ;;
        *" -X POST "*"/api/system/drain?"*)
            printf '{"ok":true,"active":true,"activeProcesses":0,"operation_id":"%s"}\n200' "$operation_id"
            ;;
        *)
            printf '{"active":true,"activeProcesses":0,"operation_id":"%s"}\n200' "$operation_id"
            ;;
    esac
}
drain_and_wait restart 9999
""",
        env={"CURL_TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert "api/system/drain/ready?" in trace.read_text()
    assert '"operation_id":"' in trace.read_text()
    assert "-X DELETE" not in trace.read_text()
    assert "api/system/drain/hard-cap?" not in trace.read_text()


def test_drain_cancels_timeout_when_hard_cap_reports_not_ready(tmp_path: Path) -> None:
    trace = tmp_path / "curl.log"
    result = _run_bash(
        _RUNTIME_PREAMBLE
        + """
curl() {
    printf '%s\\n' "$*" >> "$CURL_TRACE"
    case " $* " in
        *" -X POST "*"/api/system/drain/hard-cap?"*)
            printf '{"ok":true,"force_stop_ready":false,"operation_id":"%s"}\n200' "$operation_id"
            ;;
        *" -X POST "*"/api/system/drain?"*)
            printf '{"ok":true,"active":true,"activeProcesses":1,"operation_id":"%s"}\n200' "$operation_id"
            ;;
        *)
            printf '{"active":true,"activeProcesses":1,"operation_id":"%s"}\n200' "$operation_id"
            ;;
    esac
}
sleep() { :; }
CORSIA_DRAIN_MAX_WAIT=2
drain_and_wait restart 9999
""",
        env={"CURL_TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert "-X DELETE" in trace.read_text()
    assert "api/system/drain/hard-cap?" in trace.read_text()


_SERVICE_RESTART_PREAMBLE = """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
BACKEND_PORT=9999
# Single canonical backend port only.
CORSIA_TOKEN=test-token
FORCE=false
source scripts/corsia.d/service.sh
launchctl_bootout_wait() { launchctl bootout --wait "$1"; }
SERVICE_PLIST="${LAUNCHCTL_TRACE}.plist"
printf 'old plist\\n' > "$SERVICE_PLIST"
service_installed() { return 0; }
require_owned_service_definition() { return 0; }
service_install() { printf 'new plist\\n' > "$SERVICE_PLIST"; }
service_loaded_definition_matches() { return 1; }
service_loaded_pid() { printf '4242\n'; }
backend_pids() { printf '4242\n'; }
is_backend_running() { return 0; }
SERVICE_LOADED_CHECKS=0
service_is_loaded() {
    SERVICE_LOADED_CHECKS=$((SERVICE_LOADED_CHECKS + 1))
    [ "$SERVICE_LOADED_CHECKS" -eq 1 ]
}
service_domain() { printf 'gui/501'; }
service_label() { printf 'com.corsia.prod'; }
service_plist_path() { printf '%s' "$SERVICE_PLIST"; }
drain_and_wait() { return 0; }
wait_for_backend_health() { printf 'health\\n' >> "$LAUNCHCTL_TRACE"; return "${HEALTH_RC:-0}"; }
"""


def test_service_restart_marks_its_definition_refresh_as_internal(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "launchctl.log"
    result = _run_bash(
        _SERVICE_RESTART_PREAMBLE
        + """
service_install() {
    printf 'install-mode=%s\\n' "${1:-direct}" >> "$LAUNCHCTL_TRACE"
    printf 'new plist\\n' > "$SERVICE_PLIST"
}
launchctl() { printf '%s\\n' "$*" >> "$LAUNCHCTL_TRACE"; }
service_restart
""",
        env={"LAUNCHCTL_TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == [
        "install-mode=restart",
        "bootout --wait gui/501/com.corsia.prod",
        f"bootstrap gui/501 {trace}.plist",
    ]


def test_unknown_lifecycle_argument_exits_nonzero() -> None:
    """An argument the verb does not accept must fail loudly.

    `fail` only echoes. Under `set -e` a dispatch branch ending in `fail`
    returned echo's exit 0, so a mistyped command printed an error and then
    reported success for a command that did nothing. A bare word and a
    mistyped option are both rejected -- accepting an unknown option would
    reintroduce exactly that fake success.
    """
    for verb, extra in (
        ("start", "server"),
        ("stop", "bogus"),
        ("restart", "all"),
        ("stop", "--forse"),
        ("restart", "--bogus"),
    ):
        result = _run_bash(
            """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
source scripts/corsia.d/runtime.sh
require_no_extra_lifecycle_args """
            + verb
            + " "
            + extra
            + """ || exit 1
echo "REACHED_OPERATION"
"""
        )

        assert result.returncode != 0, (verb, extra)
        assert "REACHED_OPERATION" not in result.stdout, (verb, extra)
        assert "Unexpected argument" in result.stderr, (verb, extra)


def test_supported_lifecycle_arguments_proceed() -> None:
    """The bare verb and the documented --force flag must both proceed."""
    for args in ("restart", "stop --force", "restart --force", "start"):
        result = _run_bash(
            """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
source scripts/corsia.d/runtime.sh
require_no_extra_lifecycle_args """
            + args
            + """ || exit 1
echo "REACHED_OPERATION"
"""
        )

        assert result.returncode == 0, (args, result.stderr)
        assert "REACHED_OPERATION" in result.stdout, args


def test_service_managed_requires_a_loaded_service() -> None:
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
source scripts/corsia.d/service.sh
service_installed() { return 0; }
service_is_loaded() { return 1; }
service_managed
"""
    )

    assert result.returncode != 0


def test_service_stop_is_idempotent_when_nothing_is_loaded() -> None:
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
BACKEND_PORT=9999
source scripts/corsia.d/service.sh
service_is_loaded() { return 1; }
service_stop
"""
    )

    assert result.returncode == 0, result.stderr


_SERVICE_STOP_PREAMBLE = """
warn() { printf 'WARN: %s\\n' "$*" >&2; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
BACKEND_PORT=9999
FORCE=false
source scripts/corsia.d/service.sh
SERVICE_PLIST="${LAUNCHCTL_TRACE}.plist"
printf 'live plist\\n' > "$SERVICE_PLIST"
service_is_loaded() { return 0; }
# These tests own teardown behavior, not plist identity parsing: the sentinel
# above is deliberately not a real plist, so identity is stubbed at its seam.
service_installed() { return 0; }
require_owned_service_definition() { return 0; }
service_loaded_pid() { printf '4242\n'; }
backend_pids() { printf '4242\n'; }
is_backend_running() { return 0; }
service_domain() { printf 'gui/501'; }
service_label() { printf 'com.corsia.prod'; }
service_plist_path() { printf '%s' "$SERVICE_PLIST"; }
bootout_service_and_wait() {
    printf 'bootout %s\\n' "$2" >> "$LAUNCHCTL_TRACE"
    return 0
}
"""


def test_service_stop_stops_even_when_drain_does_not_complete(
    tmp_path: Path,
) -> None:
    """Stop is the operator's guarantee; the drain is only a bounded courtesy.

    Aborting here is what left a running service the operator could not stop:
    work that would not yield made every graceful stop fail, and the failure
    message did not even name --force.
    """
    trace = tmp_path / "launchctl.log"
    result = _run_bash(
        _SERVICE_STOP_PREAMBLE
        + """
drain_and_wait() { return 1; }
service_stop
""",
        env={"LAUNCHCTL_TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert "bootout com.corsia.prod" in trace.read_text()


def test_service_stop_keeps_the_hold_when_its_drain_fails(
    tmp_path: Path,
) -> None:
    """Releasing the hold would readmit work into the gap before teardown."""
    trace = tmp_path / "launchctl.log"
    result = _run_bash(
        _SERVICE_STOP_PREAMBLE
        + """
drain_and_wait() {
    printf 'failure-action=%s\\n' "${DRAIN_FAILURE_ACTION:-release}" \
        >> "$LAUNCHCTL_TRACE"
    return 1
}
service_stop
""",
        env={"LAUNCHCTL_TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert "failure-action=hold" in trace.read_text()


def test_service_stop_refuses_to_drain_a_listener_not_owned_by_launchd(
    tmp_path: Path,
) -> None:
    """An owned plist cannot make a foreign port listener safe to drain."""
    trace = tmp_path / "launchctl.log"
    result = _run_bash(
        _SERVICE_STOP_PREAMBLE
        + """
service_loaded_pid() { printf '777\\n'; }
backend_pids() { printf '123\\n'; }
drain_and_wait() { printf 'drain\\n' >> "$LAUNCHCTL_TRACE"; }
service_stop
""",
        env={"LAUNCHCTL_TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert not trace.exists()
    assert "does not own the backend listener" in result.stderr.lower()


def test_cancel_failed_drain_keeps_a_hold_the_caller_will_terminate(
    tmp_path: Path,
) -> None:
    """The release must be skipped without pretending it happened."""
    trace = tmp_path / "curl.log"
    result = _run_bash(
        """
warn() { printf 'WARN: %s\\n' "$*" >&2; }
ok() { :; }
fail() { :; }
BACKEND_PORT=9999
source scripts/corsia.d/runtime.sh
curl() { printf '%s\\n' "$*" >> "$CURL_TRACE"; return 0; }
DRAIN_FAILURE_ACTION=hold
cancel_failed_drain 'http://127.0.0.1:9999/api/system/drain?x=y' 'abc'
printf 'rc=%s\\n' "$?"
""",
        env={"CURL_TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert "rc=0" in result.stdout
    assert not trace.exists()


def test_lifecycle_defaults_bound_drain_and_readiness_to_one_minute() -> None:
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
BACKEND_PORT=9999
source scripts/corsia.d/runtime.sh
printf 'health=%s\\n' "$CORSIA_HEALTH_WAIT_SECONDS"
printf 'drain=%s\\n' "$CORSIA_DRAIN_MAX_WAIT"
"""
    )

    assert result.returncode == 0
    assert result.stdout.splitlines() == ["health=60", "drain=60"]


def test_backend_health_wait_uses_elapsed_wall_clock(tmp_path: Path) -> None:
    attempts = tmp_path / "health-attempts"
    clock = tmp_path / "clock"
    clock.write_text("0")
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
CORSIA_HEALTH_WAIT_SECONDS=6
source scripts/corsia.d/runtime.sh
runtime_epoch_seconds() {
    local now
    now=$(cat "$CLOCK")
    now=$((now + 4))
    printf '%s' "$now" > "$CLOCK"
    printf '%s' "$now"
}
is_backend_running() { printf 'attempt\n' >> "$ATTEMPTS"; return 1; }
sleep() { :; }
wait_for_backend_health
""",
        env={"ATTEMPTS": str(attempts), "CLOCK": str(clock)},
    )

    assert result.returncode != 0
    assert attempts.read_text().splitlines() == ["attempt", "attempt"]


def test_start_backend_delegates_readiness_to_canonical_wait(tmp_path: Path) -> None:
    trace = tmp_path / "start.log"
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
BACKEND_PORT=9999
BACKEND_DIR="$TEST_BACKEND_DIR"
BACKEND_LOG="$TEST_BACKEND_LOG"
PYTHON=/usr/bin/true
source scripts/corsia.d/runtime.sh
service_installed() { return 1; }
backend_pids() { return 0; }
is_backend_running() { printf 'probe\\n' >> "$TRACE"; return 1; }
wait_for_backend_health() { printf 'health\\n' >> "$TRACE"; }
print_backend_status() { printf 'status\\n' >> "$TRACE"; }
sleep() { :; }
start_server
""",
        env={
            "TEST_BACKEND_DIR": str(tmp_path),
            "TEST_BACKEND_LOG": str(tmp_path / "backend.log"),
            "TRACE": str(trace),
        },
    )

    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == ["probe", "health", "status"]


def test_legacy_backend_does_not_inherit_request_handoff_variables(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "start-env.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_nohup = fake_bin / "nohup"
    fake_nohup.write_text(
        "#!/bin/sh\n"
        "printf 'port=%s\\n' \"${CORSIA_RESTART_BACKEND_PORT-unset}\" >> \"$TRACE\"\n"
        "printf 'operation=%s\\n' \"${CORSIA_RESTART_OPERATION_ID-unset}\" >> \"$TRACE\"\n"
    )
    fake_nohup.chmod(0o755)
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
BACKEND_PORT=9999
BACKEND_DIR="$TEST_BACKEND_DIR"
BACKEND_LOG="$TEST_BACKEND_LOG"
PYTHON=/usr/bin/true
export CORSIA_RESTART_BACKEND_PORT=9999
export CORSIA_RESTART_OPERATION_ID=request-operation
source scripts/corsia.d/runtime.sh
is_backend_running() { return 1; }
backend_pids() { return 0; }
start_backend
wait
""",
        env={
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "TEST_BACKEND_DIR": str(tmp_path),
            "TEST_BACKEND_LOG": str(tmp_path / "backend.log"),
            "TRACE": str(trace),
        },
    )

    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == ["port=unset", "operation=unset"]


def test_start_server_uses_installed_service_even_when_unloaded(tmp_path: Path) -> None:
    trace = tmp_path / "start.log"
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
BACKEND_PORT=9999
source scripts/corsia.d/runtime.sh
service_installed() { return 0; }
service_managed() { return 1; }
service_start() { printf 'service-start\\n' >> "$TRACE"; }
start_backend() { printf 'legacy-start\\n' >> "$TRACE"; }
wait_for_backend_health() { printf 'health\\n' >> "$TRACE"; }
print_backend_status() { printf 'status\\n' >> "$TRACE"; }
start_server
""",
        env={"TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == [
        "service-start",
        "health",
        "status",
    ]


def test_bounce_server_uses_installed_service_even_when_unloaded(tmp_path: Path) -> None:
    trace = tmp_path / "bounce.log"
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
BACKEND_PORT=9999
FORCE=true
source scripts/corsia.d/runtime.sh
service_installed() { return 0; }
service_managed() { return 1; }
service_restart() { printf 'service-restart\n' >> "$TRACE"; }
is_backend_running() { return 1; }
stop_backend() { printf 'legacy-stop\n' >> "$TRACE"; }
start_backend() { printf 'legacy-start\n' >> "$TRACE"; }
wait_for_backend_health() { printf 'health\n' >> "$TRACE"; }
prove_backend_replaced() { printf 'replaced\n' >> "$TRACE"; }
print_backend_status() { printf 'status\n' >> "$TRACE"; }
sleep() { :; }
bounce_server
""",
        env={"TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == [
        "service-restart",
        "health",
        "replaced",
        "status",
    ]


def test_bounce_server_refuses_a_port_owned_by_another_checkouts_service(
    tmp_path: Path,
) -> None:
    """A loaded job under this label that is not ours still owns the port.

    Restarting it through launchd is not this checkout's business, and killing
    its process directly would only have KeepAlive bring it back. The refusal
    has to land before any stop, start, or launchd verb.
    """
    trace = tmp_path / "bounce.log"
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "com.corsia.prod.plist").write_text("another checkout's plist\n")
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
BACKEND_PORT=9999
FORCE=true
LAUNCH_AGENTS_DIR="$AGENTS_DIR"
# The port guard reads this checkout's launchd identity, so load the pair the
# CLI itself loads rather than half of it.
source scripts/corsia.d/runtime.sh
source scripts/corsia.d/service.sh
service_is_loaded() { return 0; }
service_installed() { return 1; }
service_loaded_pid() { printf '4242\\n'; }
backend_pids() { printf '4242\\n'; }
service_restart() { printf 'service-restart\\n' >> "$TRACE"; return 1; }
is_backend_running() { return 1; }
stop_backend() { printf 'legacy-stop\\n' >> "$TRACE"; }
start_backend() { printf 'legacy-start\\n' >> "$TRACE"; }
bounce_server
""",
        env={"AGENTS_DIR": str(agents_dir), "TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert not trace.exists()
    assert "another checkout" in result.stderr


def test_direct_lifecycle_refuses_loaded_service_with_unattributable_pid(
    tmp_path: Path,
) -> None:
    """A loaded launchd job must be ruled out before cwd can authorize a kill."""
    backend = tmp_path / "checkout" / "backend"
    backend.mkdir(parents=True)
    trace = tmp_path / "effects.log"
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
BACKEND_PORT=9999
BACKEND_DIR="$BACKEND"
source scripts/corsia.d/runtime.sh
source scripts/corsia.d/service.sh
service_installed() { return 1; }
service_is_loaded() { return 0; }
service_loaded_pid() { return 1; }
backend_pids() { printf '4242\\n'; }
backend_pid_working_directory() { printf '%s\\n' "$BACKEND"; }
is_backend_running() { return 1; }
stop_backend() { printf 'STOP\\n' >> "$TRACE"; }
start_backend() { printf 'START\\n' >> "$TRACE"; }
wait_for_backend_health() { :; }
prove_backend_replaced() { :; }
print_backend_status() { :; }
sleep() { :; }
bounce_server
""",
        env={"BACKEND": str(backend), "TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert not trace.exists()
    assert "cannot prove which process belongs to it" in result.stderr.lower()


def test_direct_lifecycle_refuses_loaded_service_with_empty_pid(
    tmp_path: Path,
) -> None:
    """A loaded job with no reported PID cannot authorize a direct kill."""
    backend = tmp_path / "checkout" / "backend"
    backend.mkdir(parents=True)
    trace = tmp_path / "effects.log"
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
BACKEND_PORT=9999
BACKEND_DIR="$BACKEND"
source scripts/corsia.d/runtime.sh
source scripts/corsia.d/service.sh
service_installed() { return 1; }
service_is_loaded() { return 0; }
service_loaded_pid() { return 0; }
backend_pids() { printf '4242\\n'; }
backend_pid_working_directory() { printf '%s\\n' "$BACKEND"; }
is_backend_running() { return 1; }
stop_backend() { printf 'STOP\\n' >> "$TRACE"; }
start_backend() { printf 'START\\n' >> "$TRACE"; }
wait_for_backend_health() { :; }
prove_backend_replaced() { :; }
print_backend_status() { :; }
sleep() { :; }
bounce_server
""",
        env={"BACKEND": str(backend), "TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert not trace.exists()
    assert "cannot prove which process belongs to it" in result.stderr.lower()


def test_direct_lifecycle_refuses_loaded_service_with_malformed_pid(
    tmp_path: Path,
) -> None:
    """A nonnumeric launchd PID is not process-ownership evidence."""
    backend = tmp_path / "checkout" / "backend"
    backend.mkdir(parents=True)
    trace = tmp_path / "effects.log"
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
BACKEND_PORT=9999
BACKEND_DIR="$BACKEND"
source scripts/corsia.d/runtime.sh
source scripts/corsia.d/service.sh
service_installed() { return 1; }
service_is_loaded() { return 0; }
service_loaded_pid() { printf 'not-a-pid\\n'; }
backend_pids() { printf '4242\\n'; }
backend_pid_working_directory() { printf '%s\\n' "$BACKEND"; }
is_backend_running() { return 1; }
stop_backend() { printf 'STOP\\n' >> "$TRACE"; }
start_backend() { printf 'START\\n' >> "$TRACE"; }
wait_for_backend_health() { :; }
prove_backend_replaced() { :; }
print_backend_status() { :; }
sleep() { :; }
bounce_server
""",
        env={"BACKEND": str(backend), "TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert not trace.exists()
    assert "cannot prove which process belongs to it" in result.stderr.lower()


def test_restart_prepared_proves_listener_before_drain(tmp_path: Path) -> None:
    """Update may not steer a backend until the port is proved to be ours."""
    mine = tmp_path / "mine" / "backend"
    other = tmp_path / "other" / "backend"
    checkout = tmp_path / "checkout"
    mine.mkdir(parents=True)
    other.mkdir(parents=True)
    checkout.mkdir()
    trace = tmp_path / "effects.log"

    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
BACKEND_PORT=9999
BACKEND_DIR="$MINE"
FORCE=false
SELF_CHECKOUT="$CHECKOUT"
source scripts/corsia.d/runtime.sh
source scripts/corsia.d/service.sh
service_installed() { return 1; }
service_is_loaded() { return 1; }
backend_pids() { printf '123\\n'; }
backend_pid_working_directory() { printf '%s\\n' "$OTHER"; }
is_backend_running() { return 0; }
read_backend_start_iso() { printf '2026-01-01T00:00:00Z\\n'; }
drain_and_wait() { printf 'DRAIN\\n' >> "$TRACE"; return 0; }
restart_prepared
""",
        env={
            "CHECKOUT": str(checkout),
            "MINE": str(mine),
            "OTHER": str(other),
            "TRACE": str(trace),
        },
    )

    assert result.returncode != 0
    assert not trace.exists()
    assert "another checkout" in result.stderr


def test_restart_prepared_proves_loaded_service_owns_listener_before_drain(
    tmp_path: Path,
) -> None:
    """An owned loaded plist cannot authorize draining another port listener."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    trace = tmp_path / "effects.log"

    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
BACKEND_PORT=9999
BACKEND_DIR="$CHECKOUT/backend"
FORCE=false
SELF_CHECKOUT="$CHECKOUT"
source scripts/corsia.d/runtime.sh
source scripts/corsia.d/service.sh
service_installed() { return 0; }
require_owned_service_definition() { return 0; }
service_is_loaded() { return 0; }
service_loaded_pid() { printf '777\\n'; }
backend_pids() { printf '123\\n'; }
is_backend_running() { return 0; }
read_backend_start_iso() { printf '2026-01-01T00:00:00Z\\n'; }
drain_and_wait() { printf 'DRAIN\\n' >> "$TRACE"; return 0; }
bounce_server() { printf 'BOUNCE\\n' >> "$TRACE"; return 0; }
restart_prepared
""",
        env={"CHECKOUT": str(checkout), "TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert not trace.exists()
    assert "does not own the backend listener" in result.stderr.lower()


def test_forced_restart_prepared_recovers_loaded_service_without_listener(
    tmp_path: Path,
) -> None:
    """Force recovery may bounce an owned idle launchd job without a port target."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    trace = tmp_path / "effects.log"

    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
BACKEND_PORT=9999
BACKEND_DIR="$CHECKOUT/backend"
FORCE=true
SELF_CHECKOUT="$CHECKOUT"
source scripts/corsia.d/runtime.sh
source scripts/corsia.d/service.sh
service_installed() { return 0; }
require_owned_service_definition() { return 0; }
service_is_loaded() { return 0; }
service_loaded_pid() { return 0; }
backend_pids() { return 0; }
is_backend_running() { return 1; }
bounce_server() { printf 'BOUNCE\\n' >> "$TRACE"; return 0; }
restart_prepared
""",
        env={"CHECKOUT": str(checkout), "TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == ["BOUNCE"]


def test_unloaded_service_refuses_to_race_a_live_backend(tmp_path: Path) -> None:
    trace = tmp_path / "service.log"
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
BACKEND_PORT=9999
FORCE=true
source scripts/corsia.d/service.sh
service_installed() { return 0; }
require_owned_service_definition() { return 0; }
service_is_loaded() { return 1; }
is_backend_running() { return 0; }
backend_pids() { printf '123\\n'; }
service_install() { printf 'install\\n' >> "$TRACE"; }
service_start() { printf 'start\\n' >> "$TRACE"; }
service_restart
""",
        env={"TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert not trace.exists()


def test_unloaded_service_activates_release_before_install(tmp_path: Path) -> None:
    trace = tmp_path / "service.log"
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
BACKEND_PORT=9999
FORCE=true
source scripts/corsia.d/service.sh
service_installed() { return 0; }
require_owned_service_definition() { return 0; }
service_is_loaded() { return 1; }
is_backend_running() { return 1; }
backend_pids() { return 0; }
activate_release() { printf 'activate\\n' >> "$TRACE"; }
service_install() { printf 'install\\n' >> "$TRACE"; }
service_start() { printf 'start\\n' >> "$TRACE"; }
service_restart activate_release
""",
        env={"TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == ["activate", "install", "start"]


def test_start_server_proves_health_before_reporting_runtime(tmp_path: Path) -> None:
    trace = tmp_path / "start.log"
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
BACKEND_PORT=9999
source scripts/corsia.d/runtime.sh
service_installed() { return 1; }
start_backend() { printf 'start\\n' >> "$TRACE"; }
wait_for_backend_health() { printf 'health\\n' >> "$TRACE"; }
print_backend_status() { printf 'status\\n' >> "$TRACE"; }
start_server
""",
        env={"TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == ["start", "health", "status"]


def test_bounce_server_fails_before_stopping_when_identity_is_unreadable(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "bounce.log"
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
BACKEND_PORT=9999
FORCE=true
source scripts/corsia.d/runtime.sh
is_backend_running() { return 0; }
read_backend_start_iso() { return 1; }
service_managed() { return 1; }
stop_backend() { printf 'stop\n' >> "$TRACE"; }
start_backend() { printf 'start\n' >> "$TRACE"; }
wait_for_backend_health() { :; }
print_backend_status() { :; }
sleep() { :; }
bounce_server
""",
        env={"TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert not trace.exists()


def test_bounce_server_rejects_the_original_healthy_process(tmp_path: Path) -> None:
    trace = tmp_path / "bounce.log"
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
BACKEND_PORT=9999
FORCE=true
source scripts/corsia.d/runtime.sh
resolve_bind_host() { echo 0.0.0.0; }
is_backend_running() { return 0; }
service_managed() { return 1; }
read_backend_start_iso() { printf 'same-start'; }
stop_backend() { printf 'stop\n' >> "$TRACE"; }
start_backend() { printf 'start\n' >> "$TRACE"; }
wait_for_backend_health() { printf 'health\n' >> "$TRACE"; }
print_backend_status() { printf 'status\n' >> "$TRACE"; }
sleep() { :; }
bounce_server
""",
        env={"TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert trace.read_text().splitlines() == ["stop", "start", "health"]


def test_legacy_bounce_uses_canonical_restart_reason(tmp_path: Path) -> None:
    trace = tmp_path / "bounce.log"
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
BACKEND_PORT=9999
FORCE=false
source scripts/corsia.d/runtime.sh
resolve_bind_host() { echo 0.0.0.0; }
is_backend_running() { return 0; }
read_backend_start_iso() { printf 'before'; }
service_installed() { return 1; }
drain_and_wait() { printf '%s\\n' "$1" >> "$TRACE"; return 1; }
bounce_server
""",
        env={"TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert trace.read_text().splitlines() == ["restart"]


def test_start_server_never_reports_ready_after_failed_health(tmp_path: Path) -> None:
    trace = tmp_path / "start.log"
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
BACKEND_PORT=9999
source scripts/corsia.d/runtime.sh
service_installed() { return 1; }
start_backend() { printf 'start\\n' >> "$TRACE"; }
wait_for_backend_health() { printf 'health\\n' >> "$TRACE"; return 1; }
print_backend_status() { printf 'status\\n' >> "$TRACE"; }
start_server
""",
        env={"TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert trace.read_text().splitlines() == ["start", "health"]


def test_backend_status_reports_exact_runtime_identity() -> None:
    result = _run_bash(
        """
ok() { printf 'OK: %s\\n' "$*"; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
BACKEND_PORT=9999
SELF_CHECKOUT=/tmp/corsia-dev
source scripts/corsia.d/runtime.sh
curl() {
    printf '%s' '{"frontend":{"full_hash":"abcdef1234567890"},"backend":{"full_hash":"abcdef1234567890","current_head":{"full_hash":"abcdef1234567890"}}}'
}
service_managed() { return 1; }
backend_pids() { printf '4321\\n'; }
print_backend_status
"""
    )

    assert result.returncode == 0, result.stderr
    assert "Health: ok" in result.stdout
    assert "Serving on :9999" in result.stdout
    assert "Checkout: /tmp/corsia-dev" in result.stdout
    assert "Revision: abcdef123456 (backend + frontend)" in result.stdout
    assert "Process: legacy process PID 4321" in result.stdout


def test_backend_status_rejects_mixed_runtime_revisions() -> None:
    result = _run_bash(
        """
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
BACKEND_PORT=9999
source scripts/corsia.d/runtime.sh
curl() {
    printf '%s' '{"frontend":{"full_hash":"frontend-sha"},"backend":{"full_hash":"backend-sha","current_head":{"full_hash":"backend-sha"}}}'
}
print_backend_status
"""
    )

    assert result.returncode != 0
    assert "backend, frontend, and checkout revisions do not agree" in result.stderr


def test_service_restart_bootstraps_without_retry_on_immediate_success(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "launchctl.log"
    result = _run_bash(
        _SERVICE_RESTART_PREAMBLE
        + """
launchctl() { printf '%s\\n' "$*" >> "$LAUNCHCTL_TRACE"; }
sleep() { printf 'sleep %s\\n' "$*" >> "$LAUNCHCTL_TRACE"; }
service_restart
""",
        env={"LAUNCHCTL_TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == [
        "bootout --wait gui/501/com.corsia.prod",
        f"bootstrap gui/501 {trace}.plist",
    ]


def test_service_restart_activates_release_after_bootout_before_bootstrap(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "launchctl.log"
    result = _run_bash(
        _SERVICE_RESTART_PREAMBLE
        + """
launchctl() { printf '%s\\n' "$*" >> "$LAUNCHCTL_TRACE"; }
activate_release() { printf 'activate release\\n' >> "$LAUNCHCTL_TRACE"; }
service_restart activate_release
""",
        env={"LAUNCHCTL_TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == [
        "bootout --wait gui/501/com.corsia.prod",
        "activate release",
        f"bootstrap gui/501 {trace}.plist",
    ]


def test_service_restart_refuses_a_stale_loaded_generation_before_any_effect(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "launchctl.log"
    plist = tmp_path / "service.plist"
    with plist.open("wb") as plist_file:
        plistlib.dump(
            {
                "Label": "com.corsia.prod",
                "ProgramArguments": ["/usr/bin/python3"],
                "WorkingDirectory": "/tmp/corsia/backend",
                "EnvironmentVariables": {
                    "BACKEND_PORT": "9999",
                    "CORSIA_SERVICE_GENERATION": "installed-generation",
                    "CORSIA_WORKSPACE": str(tmp_path / "workspace"),
                    "CORSIA_STATE_DIR": str(tmp_path / "state"),
                },
            },
            plist_file,
        )
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { printf '%s\\n' "$*" >&2; }
BACKEND_PORT=9999
BACKEND_DIR=/tmp/corsia/backend
PYTHON=/usr/bin/python3
FORCE=true
CORSIA_WORKSPACE="$TEST_WORKSPACE"
CORSIA_STATE_DIR="$TEST_STATE"
source scripts/corsia.d/service.sh
service_plist_path() { printf '%s' "$SERVICE_PLIST"; }
service_installed() { return 0; }
service_is_loaded() { return 0; }
service_loaded_definition_matches() { return 1; }
service_install() { printf 'install\\n' >> "$TRACE"; }
drain_and_wait() { printf 'drain\\n' >> "$TRACE"; }
launchctl() { printf 'launchctl %s\\n' "$*" >> "$TRACE"; }
service_restart
""",
        env={
            "SERVICE_PLIST": str(plist),
            "TEST_STATE": str(tmp_path / "state"),
            "TEST_WORKSPACE": str(tmp_path / "workspace"),
            "TRACE": str(trace),
        },
    )

    assert result.returncode != 0
    assert (
        "Loaded com.corsia.prod generation does not match its installed "
        "service definition" in result.stderr
    )
    assert not trace.exists()


def test_service_start_refuses_to_bootstrap_over_a_live_legacy_backend(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "launchctl.log"
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { printf '%s\\n' "$*" >&2; }
BACKEND_PORT=9999
source scripts/corsia.d/service.sh
service_installed() { return 0; }
require_owned_service_definition() { return 0; }
service_is_loaded() { return 1; }
is_backend_running() { return 0; }
backend_pids() { printf '123\\n'; }
launchctl() { printf '%s\\n' "$*" >> "$TRACE"; }
service_start
""",
        env={"TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert "refusing to race it" in result.stderr
    assert not trace.exists()


def test_service_start_reports_that_it_reuses_the_installed_definition(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "launchctl.log"
    result = _run_bash(
        """
warn() { :; }
ok() { printf '%s\\n' "$*"; }
fail() { printf '%s\\n' "$*" >&2; }
BACKEND_PORT=9999
source scripts/corsia.d/service.sh
service_installed() { return 0; }
require_owned_service_definition() { return 0; }
service_is_loaded() { return 1; }
is_backend_running() { return 1; }
backend_pids() { :; }
service_domain() { printf 'gui/501'; }
service_plist_path() { printf '/tmp/com.corsia.test.plist'; }
launchctl() { printf '%s\\n' "$*" >> "$TRACE"; }
service_start
""",
        env={"TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == [
        "bootstrap gui/501 /tmp/com.corsia.test.plist",
    ]
    assert "using the installed definition" in result.stdout
    assert "service install" in result.stdout
    assert "service restart" in result.stdout


def test_service_stop_reports_that_it_preserves_the_installed_definition() -> None:
    result = _run_bash(
        """
warn() { :; }
ok() { printf '%s\\n' "$*"; }
fail() { printf '%s\\n' "$*" >&2; }
BACKEND_PORT=9999
FORCE=true
source scripts/corsia.d/service.sh
service_label() { printf 'com.corsia.test'; }
service_domain() { printf 'gui/501'; }
service_plist_path() { printf '/tmp/com.corsia.test.plist'; }
service_is_loaded() { return 0; }
service_installed() { return 0; }
require_owned_service_definition() { return 0; }
bootout_service_and_wait() { return 0; }
service_stop
""",
    )

    assert result.returncode == 0, result.stderr
    assert "installed definition preserved" in result.stdout
    assert "service install" in result.stdout
    assert "service restart" in result.stdout


def test_service_restart_refuses_to_drain_a_listener_not_owned_by_launchd(
    tmp_path: Path,
) -> None:
    """Restart refuses before replacing the plist or draining a foreign listener."""
    trace = tmp_path / "launchctl.log"
    result = _run_bash(
        _SERVICE_RESTART_PREAMBLE
        + """
service_loaded_pid() { printf '777\\n'; }
backend_pids() { printf '123\\n'; }
drain_and_wait() { printf 'drain\\n' >> "$LAUNCHCTL_TRACE"; }
service_restart
""",
        env={"LAUNCHCTL_TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert Path(f"{trace}.plist").read_text() == "old plist\n"
    assert not trace.exists()
    assert "does not own the backend listener" in result.stderr.lower()


def test_service_restart_installs_definition_before_drain(tmp_path: Path) -> None:
    trace = tmp_path / "launchctl.log"
    result = _run_bash(
        _SERVICE_RESTART_PREAMBLE
        + """
service_install() { printf 'install\\n' >> "$LAUNCHCTL_TRACE"; }
drain_and_wait() { printf 'drain\\n' >> "$LAUNCHCTL_TRACE"; }
launchctl() { printf '%s\\n' "$*" >> "$LAUNCHCTL_TRACE"; }
service_restart
""",
        env={"LAUNCHCTL_TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == [
        "install",
        "drain",
        "bootout --wait gui/501/com.corsia.prod",
        f"bootstrap gui/501 {trace}.plist",
    ]


def test_service_restart_proceeds_to_teardown_when_drain_fails(
    tmp_path: Path,
) -> None:
    """An incomplete drain must not leave the old generation serving.

    Draining is a bounded courtesy; replacing the generation is the promise.
    This used to restore the prior definition and return failure, so a machine
    holding work that would not yield could never be restarted at all.
    """
    trace = tmp_path / "launchctl.log"
    result = _run_bash(
        _SERVICE_RESTART_PREAMBLE
        + """
drain_and_wait() { return 1; }
launchctl() { printf '%s\\n' "$*" >> "$LAUNCHCTL_TRACE"; }
service_restart
""",
        env={"LAUNCHCTL_TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert Path(f"{trace}.plist").read_text() == "new plist\n"
    assert not list(tmp_path.glob("launchctl.log.plist.previous.*"))
    assert not list(tmp_path.glob("launchctl.log.plist.restore.*"))
    assert "bootout" in trace.read_text()


def test_service_restart_keeps_the_hold_when_its_drain_fails(
    tmp_path: Path,
) -> None:
    """Releasing the hold would readmit work into the gap before teardown."""
    trace = tmp_path / "launchctl.log"
    result = _run_bash(
        _SERVICE_RESTART_PREAMBLE
        + """
drain_and_wait() {
    printf 'failure-action=%s\\n' "${DRAIN_FAILURE_ACTION:-release}" \
        >> "$LAUNCHCTL_TRACE"
    return 1
}
launchctl() { printf '%s\\n' "$*" >> "$LAUNCHCTL_TRACE"; }
service_restart
""",
        env={"LAUNCHCTL_TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert "failure-action=hold" in trace.read_text()


def test_service_restart_render_failure_never_begins_drain(
    tmp_path: Path,
    isolated_launch_agents_dir: Path,
) -> None:
    plist = isolated_launch_agents_dir / "com.corsia.prod.plist"
    plist.write_text("live plist\n")
    trace = tmp_path / "service.log"
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
BACKEND_PORT=9999
BACKEND_DIR=/tmp/corsia/backend
BACKEND_LOG=/tmp/corsia/backend.log
PYTHON=
ENV_FILE=/tmp/corsia-no-such-env
FORCE=false
source scripts/corsia.d/service.sh
require_owned_service_definition() { return 0; }
service_installed() { return 0; }
service_is_loaded() { return 0; }
drain_and_wait() { printf 'drain\\n' >> "$SERVICE_TRACE"; }
launchctl() { printf '%s\\n' "$*" >> "$SERVICE_TRACE"; }
service_restart
""",
        env={
            "LAUNCH_AGENTS_DIR": str(isolated_launch_agents_dir),
            "SERVICE_TRACE": str(trace),
        },
    )

    assert result.returncode != 0
    assert "Service plist needs an interpreter" in result.stderr
    assert plist.read_text() == "live plist\n"
    assert not trace.exists()


def test_preserved_service_restore_is_atomic_when_copy_fails(tmp_path: Path) -> None:
    installed = tmp_path / "installed.plist"
    previous = tmp_path / "previous.plist"
    installed.write_text("candidate plist\n")
    previous.write_text("prior plist\n")

    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
source scripts/corsia.d/service.sh
cp() {
    printf 'partial restore\n' > "$3"
    return 1
}
install_preserved_service_definition "$PREVIOUS" "$INSTALLED"
""",
        env={"INSTALLED": str(installed), "PREVIOUS": str(previous)},
    )

    assert result.returncode != 0
    assert installed.read_text() == "candidate plist\n"
    assert previous.read_text() == "prior plist\n"
    assert not list(tmp_path.glob("installed.plist.restore.*"))


def test_service_restart_retries_transient_bootstrap_status_5(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "launchctl.log"
    result = _run_bash(
        _SERVICE_RESTART_PREAMBLE
        + """
launchctl() {
    printf '%s\\n' "$*" >> "$LAUNCHCTL_TRACE"
    [ "$1" = bootstrap ] || return 0
    attempts="$(grep -c '^bootstrap ' "$LAUNCHCTL_TRACE")"
    if [ "$attempts" -eq 1 ]; then
        printf '%s\\n' 'Bootstrap failed: 5: Input/output error' >&2
        return 5
    fi
}
sleep() { printf 'sleep %s\\n' "$*" >> "$LAUNCHCTL_TRACE"; }
service_restart
""",
        env={"LAUNCHCTL_TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == [
        "bootout --wait gui/501/com.corsia.prod",
        f"bootstrap gui/501 {trace}.plist",
        "sleep 1",
        f"bootstrap gui/501 {trace}.plist",
    ]


def test_restored_prior_definition_that_stays_unhealthy_reports_an_outage(
    tmp_path: Path,
) -> None:
    """A restore that never becomes healthy is an outage, not a rollback.

    Covers #1364's remaining failure branch: reinstalling and bootstrapping
    the prior definition can still leave production down, and that outcome
    must be reported as unavailability rather than a successful rollback.
    """
    trace = tmp_path / "launchctl.log"

    result = _run_bash(
        _SERVICE_RESTART_PREAMBLE
        + """
CORSIA_LAUNCHD_BOOTSTRAP_ATTEMPTS=2
launchctl() {
    printf '%s\\n' "$*" >> "$LAUNCHCTL_TRACE"
    [ "$1" = bootstrap ] || return 0
    grep -q '^old plist$' "$SERVICE_PLIST" && return 0
    printf '%s\\n' 'Bootstrap failed: 5: Input/output error' >&2
    return 5
}
sleep() { :; }
service_restart
""",
        env={"LAUNCHCTL_TRACE": str(trace), "HEALTH_RC": "1"},
    )

    assert result.returncode != 0
    assert "Production unavailable" in result.stderr
    assert "did not become healthy" in result.stderr
    # The prior definition is still what is installed, so recovery is possible.
    assert Path(f"{trace}.plist").read_text() == "old plist\n"


def test_service_restart_accepts_exact_replacement_after_bootstrap_status_5(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "launchctl.log"
    result = _run_bash(
        _SERVICE_RESTART_PREAMBLE
        + """
service_loaded_definition_matches() { return 0; }
launchctl() {
    printf '%s\\n' "$*" >> "$LAUNCHCTL_TRACE"
    [ "$1" = bootstrap ] || return 0
    printf '%s\\n' 'Bootstrap failed: 5: Input/output error' >&2
    return 5
}
sleep() { printf 'sleep %s\\n' "$*" >> "$LAUNCHCTL_TRACE"; }
service_restart
""",
        env={"LAUNCHCTL_TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == [
        "bootout --wait gui/501/com.corsia.prod",
        f"bootstrap gui/501 {trace}.plist",
    ]


def test_service_restart_evicts_stale_loaded_definition_after_status_5(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "launchctl.log"
    result = _run_bash(
        _SERVICE_RESTART_PREAMBLE
        + """
service_is_loaded() { return 0; }
launchctl() {
    printf '%s\\n' "$*" >> "$LAUNCHCTL_TRACE"
    [ "$1" = bootstrap ] || return 0
    attempts="$(grep -c '^bootstrap ' "$LAUNCHCTL_TRACE")"
    if [ "$attempts" -eq 1 ]; then
        printf '%s\\n' 'Bootstrap failed: 5: Input/output error' >&2
        return 5
    fi
}
sleep() { printf 'sleep %s\\n' "$*" >> "$LAUNCHCTL_TRACE"; }
service_restart
""",
        env={"LAUNCHCTL_TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == [
        "bootout --wait gui/501/com.corsia.prod",
        f"bootstrap gui/501 {trace}.plist",
        "bootout --wait gui/501/com.corsia.prod",
        "sleep 1",
        f"bootstrap gui/501 {trace}.plist",
    ]


def test_service_restart_restores_prior_definition_after_replacement_exhaustion(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "launchctl.log"
    result = _run_bash(
        _SERVICE_RESTART_PREAMBLE
        + """
launchctl() {
    printf '%s\\n' "$*" >> "$LAUNCHCTL_TRACE"
    [ "$1" = bootstrap ] || return 0
    grep -q '^old plist$' "$SERVICE_PLIST" && return 0
    printf '%s\\n' 'Bootstrap failed: 5: Input/output error' >&2
    return 5
}
sleep() { printf 'sleep %s\\n' "$*" >> "$LAUNCHCTL_TRACE"; }
CORSIA_LAUNCHD_BOOTSTRAP_ATTEMPTS=2
service_restart
""",
        env={"LAUNCHCTL_TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert Path(f"{trace}.plist").read_text() == "old plist\n"
    assert not list(tmp_path.glob("launchctl.log.plist.previous.*"))
    assert trace.read_text().splitlines() == [
        "bootout --wait gui/501/com.corsia.prod",
        f"bootstrap gui/501 {trace}.plist",
        "sleep 1",
        f"bootstrap gui/501 {trace}.plist",
        f"bootstrap gui/501 {trace}.plist",
        "health",
    ]


def test_update_restart_reports_recovered_prior_definition_as_failure(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "launchctl.log"
    result = _run_bash(
        _SERVICE_RESTART_PREAMBLE
        + """
launchctl() {
    printf '%s\n' "$*" >> "$LAUNCHCTL_TRACE"
    [ "$1" = bootstrap ] || return 0
    grep -q '^old plist$' "$SERVICE_PLIST" && return 0
    printf '%s\n' 'Bootstrap failed: 64: invalid request' >&2
    return 64
}
activate_release() { printf 'activate release\n' >> "$LAUNCHCTL_TRACE"; }
service_restart activate_release
""",
        env={"LAUNCHCTL_TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert Path(f"{trace}.plist").read_text() == "old plist\n"
    assert "prior service definition was restored" in result.stderr
    assert "update did not complete" in result.stderr
    assert trace.read_text().splitlines() == [
        "bootout --wait gui/501/com.corsia.prod",
        "activate release",
        f"bootstrap gui/501 {trace}.plist",
        f"bootstrap gui/501 {trace}.plist",
        "health",
    ]


def test_service_restart_reports_exhausted_bootstrap_status_5(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "launchctl.log"
    result = _run_bash(
        _SERVICE_RESTART_PREAMBLE
        + """
launchctl() {
    printf '%s\\n' "$*" >> "$LAUNCHCTL_TRACE"
    [ "$1" = bootstrap ] || return 0
    attempts="$(grep -c '^bootstrap ' "$LAUNCHCTL_TRACE")"
    printf 'attempt %s: Bootstrap failed: 5: Input/output error\\n' "$attempts" >&2
    return 5
}
sleep() { printf 'sleep %s\\n' "$*" >> "$LAUNCHCTL_TRACE"; }
CORSIA_LAUNCHD_BOOTSTRAP_ATTEMPTS=3
service_restart
""",
        env={"LAUNCHCTL_TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert trace.read_text().splitlines() == [
        "bootout --wait gui/501/com.corsia.prod",
        f"bootstrap gui/501 {trace}.plist",
        "sleep 1",
        f"bootstrap gui/501 {trace}.plist",
        "sleep 1",
        f"bootstrap gui/501 {trace}.plist",
        f"bootstrap gui/501 {trace}.plist",
    ]
    assert (
        "com.corsia.prod restart failed while loading the installed plist "
        "after 3 bootstrap attempts"
    ) in result.stderr
    assert "domain: gui/501" in result.stderr
    assert f"plist: {trace}.plist" in result.stderr
    assert (
        "Final launchctl error (exit 5): attempt 3: "
        "Bootstrap failed: 5: Input/output error"
    ) in result.stderr
    assert (
        "Manual recovery: launchctl bootstrap gui/501 "
        f"{trace}.plist"
    ) in result.stderr
    assert "production is unavailable" in result.stderr
    backups = list(tmp_path.glob("launchctl.log.plist.previous.*"))
    assert len(backups) == 1
    assert backups[0].read_text() == "old plist\n"


def test_service_restart_does_not_retry_non_5_bootstrap_failure(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "launchctl.log"
    result = _run_bash(
        _SERVICE_RESTART_PREAMBLE
        + """
launchctl() {
    printf '%s\\n' "$*" >> "$LAUNCHCTL_TRACE"
    [ "$1" = bootstrap ] || return 0
    printf '%s\\n' 'Bootstrap failed: 64: invalid request' >&2
    return 64
}
sleep() { printf 'sleep %s\\n' "$*" >> "$LAUNCHCTL_TRACE"; }
service_restart
""",
        env={"LAUNCHCTL_TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert trace.read_text().splitlines() == [
        "bootout --wait gui/501/com.corsia.prod",
        f"bootstrap gui/501 {trace}.plist",
        f"bootstrap gui/501 {trace}.plist",
    ]

@pytest.mark.parametrize("operation", ["service_stop", "service_restart"])
def test_service_mutation_failure_cancels_a_completed_drain(
    tmp_path: Path,
    operation: str,
    isolated_launch_agents_dir: Path,
) -> None:
    trace = tmp_path / "service.log"
    url_trace = tmp_path / "drain-url-helper"
    (isolated_launch_agents_dir / "com.corsia.prod.plist").write_text("installed plist")
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
BACKEND_PORT=9999
# Single canonical backend port only.
BACKEND_DIR=/tmp/corsia/backend
BACKEND_LOG=/tmp/corsia/backend.log
PYTHON=/usr/bin/python3
ENV_FILE=/tmp/corsia-no-such-env
CORSIA_TOKEN=test-token
FORCE=false
source scripts/corsia.d/service.sh
launchctl_bootout_wait() { launchctl bootout --wait "$1"; }
require_owned_service_definition() { return 0; }
service_installed() { return 0; }
service_install() { return 0; }
service_is_loaded() { return 0; }
service_loaded_pid() { printf '4242\n'; }
backend_pids() { printf '4242\n'; }
is_backend_running() { return 0; }
service_domain() { printf 'gui/501'; }
service_label() { printf 'com.corsia.prod'; }
drain_and_wait() { return 0; }
current_drain_url() {
    : > "$URL_TRACE"
    printf 'http://127.0.0.1:9999/api/system/drain?%s=%s' 'to''ken' 'test-token'
}
cancel_failed_drain() {
    if [ "$2" != request-operation ]; then
        printf 'wrong operation id: %s\\n' "$2" >> "$SERVICE_TRACE"
        return 1
    fi
    printf 'cancel %s\\n' "$1" >> "$SERVICE_TRACE"
}
launchctl() {
    [ "$1" = print ] && return 0
    printf 'launchctl %s\\n' "$*" >> "$SERVICE_TRACE"
    return 1
}
"""
        + operation,
        env={
            "LAUNCH_AGENTS_DIR": str(isolated_launch_agents_dir),
            "CORSIA_RESTART_OPERATION_ID": "request-operation",
            "SERVICE_TRACE": str(trace),
            "URL_TRACE": str(url_trace),
        },
    )

    assert result.returncode != 0
    assert url_trace.exists()
    assert trace.read_text().splitlines() == [
        "launchctl bootout --wait gui/501/com.corsia.prod",
        "cancel http://127.0.0.1:9999/api/system/drain?token=test-token",
    ]



@pytest.mark.parametrize("operation", ["service_stop", "service_restart"])
def test_service_mutation_does_not_reach_launchctl_after_failed_drain(
    tmp_path: Path,
    operation: str,
    isolated_launch_agents_dir: Path,
) -> None:
    trace = tmp_path / "launchctl.log"
    drain_trace = tmp_path / "drain.log"
    (isolated_launch_agents_dir / "com.corsia.prod.plist").write_text(
        "installed plist\n"
    )
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
BACKEND_PORT=9999
# Single canonical backend port only.
FORCE=false
source scripts/corsia.d/service.sh
require_owned_service_definition() { return 0; }
service_installed() { return 0; }
service_is_loaded() { return 0; }
service_loaded_pid() { printf '4242\n'; }
backend_pids() { printf '4242\n'; }
is_backend_running() { return 0; }
service_install() { return 0; }
drain_and_wait() { printf 'drain\\n' >> "$DRAIN_TRACE"; return 1; }
launchctl() { printf '%s\\n' "$*" >> "$LAUNCHCTL_TRACE"; }
"""
        + operation,
        env={
            "DRAIN_TRACE": str(drain_trace),
            "LAUNCHCTL_TRACE": str(trace),
        },
    )

    assert result.returncode != 0
    assert drain_trace.read_text().splitlines() == ["drain"]
    assert not trace.exists()


def test_rendered_launchd_service_has_explicit_shutdown_budget(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "corsia.env"
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    user_local_bin = home_dir / ".local" / "bin"
    env_file.write_text(
        "CORSIA_RESTART_CALLER_TOKEN=must-not-render\n"
        "CORSIA_RESTART_CURL=/tmp/must-not-render\n"
        "VISIBLE_SETTING=preserved\n"
    )
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { return 1; }
BACKEND_PORT=9999
BACKEND_DIR=/tmp/corsia/backend
BACKEND_LOG=/tmp/corsia/backend.log
# Single canonical backend port only.
# No secondary durable instance variables.
CORSIA_WORKSPACE=/tmp/corsia/workspace
CORSIA_STATE_DIR=/tmp/corsia/state
PYTHON=/usr/bin/python3
ENV_FILE="$TEST_ENV_FILE"
source scripts/corsia.d/service.sh
resolve_bind_host() { echo 0.0.0.0; }
render_service_plist generation-test
""",
        env={"HOME": str(home_dir), "TEST_ENV_FILE": str(env_file)},
    )

    assert result.returncode == 0, result.stderr
    payload = plistlib.loads(result.stdout.encode())
    assert payload["ExitTimeOut"] == 120
    environment = payload["EnvironmentVariables"]
    assert environment["CORSIA_SERVICE_GENERATION"] == "generation-test"
    assert environment["PATH"].split(":")[0] == str(user_local_bin)
    assert "~" not in environment["PATH"]
    assert environment["VISIBLE_SETTING"] == "preserved"
    assert "CORSIA_RESTART_CALLER_TOKEN" not in environment
    assert "CORSIA_RESTART_CURL" not in environment


def test_rendered_launchd_service_keeps_key_variables_for_the_key_store_migration(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "corsia.env"
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    env_file.write_text(
        "ANTHROPIC_API_KEY=anthropic-render-for-migration\n"
        "export OPENAI_API_KEY=\"openai-render-for-migration\"\n"
        "BRAVE_API_KEY=brave-render-for-migration\n"
        "VISIBLE_SETTING=preserved\n"
    )
    result = _run_bash(
        """
warn() { echo "warn: $*" >&2; }
ok() { :; }
fail() { return 1; }
BACKEND_PORT=9999
BACKEND_DIR=/tmp/corsia/backend
BACKEND_LOG=/tmp/corsia/backend.log
CORSIA_WORKSPACE=/tmp/corsia/workspace
CORSIA_STATE_DIR=/tmp/corsia/state
PYTHON=/usr/bin/python3
ENV_FILE="$TEST_ENV_FILE"
source scripts/corsia.d/service.sh
resolve_bind_host() { echo 0.0.0.0; }
render_service_plist generation-test
""",
        env={"HOME": str(home_dir), "TEST_ENV_FILE": str(env_file)},
    )

    assert result.returncode == 0, result.stderr
    environment = plistlib.loads(result.stdout.encode())["EnvironmentVariables"]
    assert environment["VISIBLE_SETTING"] == "preserved"
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "BRAVE_API_KEY"):
        assert key in environment
        assert f"{env_file} sets {key}" not in result.stderr
    assert environment["ANTHROPIC_API_KEY"] == "anthropic-render-for-migration"
    assert environment["OPENAI_API_KEY"] == "openai-render-for-migration"
    assert environment["BRAVE_API_KEY"] == "brave-render-for-migration"


def test_service_install_replaces_plist_with_owner_only_permissions(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    plist = agents_dir / "com.corsia.prod.plist"
    plist.write_text("old plist")
    plist.chmod(0o644)

    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
LAUNCH_AGENTS_DIR="$AGENTS_DIR"
source scripts/corsia.d/service.sh
render_service_plist() { printf '%s' 'new plist'; }
plutil() { return 0; }
service_is_loaded() { return 1; }
require_owned_service_definition() { return 0; }
service_install
""",
        env={"AGENTS_DIR": str(agents_dir)},
    )

    assert result.returncode == 0, result.stderr
    assert plist.read_text() == "new plist"
    assert stat.S_IMODE(plist.stat().st_mode) == 0o600


def test_direct_service_install_refuses_a_loaded_job_before_writing(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    plist = agents_dir / "com.corsia.prod.plist"
    plist.write_text("owned plist")
    trace = tmp_path / "effects.log"

    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { printf '%s\\n' "$*" >&2; }
LAUNCH_AGENTS_DIR="$AGENTS_DIR"
source scripts/corsia.d/service.sh
service_is_loaded() { return 0; }
service_installed() { return 0; }
require_owned_service_definition() { return 0; }
new_service_generation() { printf 'new-generation'; }
render_service_plist() { printf 'render\\n' >> "$TRACE"; printf 'new plist'; }
plutil() { printf 'plutil\\n' >> "$TRACE"; return 0; }
service_install
""",
        env={"AGENTS_DIR": str(agents_dir), "TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert plist.read_text() == "owned plist"
    assert not trace.exists()
    assert "corsia restart" in result.stderr


@pytest.mark.parametrize(
    ("operation", "expected_message"),
    [
        # Install is told the definition belongs to another checkout, because
        # that is the actionable fact: this checkout needs its own label.
        ("service_install", "another checkout"),
        ("service_uninstall", "unowned definition"),
    ],
)
def test_service_definition_reconciliation_refuses_a_foreign_owner(
    tmp_path: Path,
    operation: str,
    expected_message: str,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    plist = agents_dir / "com.corsia.prod.plist"
    with plist.open("wb") as plist_file:
        plistlib.dump(
            {
                "Label": "com.corsia.prod",
                "ProgramArguments": ["/foreign/python"],
                "WorkingDirectory": "/foreign/backend",
                "EnvironmentVariables": {"BACKEND_PORT": "9999"},
            },
            plist_file,
        )
    original = plist.read_bytes()
    trace = tmp_path / "effects.log"

    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { printf '%s\n' "$*" >&2; }
BACKEND_PORT=9999
BACKEND_DIR=/expected/backend
BACKEND_LOG=/tmp/corsia-backend.log
PYTHON=/usr/bin/python3
ENV_FILE=/tmp/corsia-no-such-env
LAUNCH_AGENTS_DIR="$AGENTS_DIR"
source scripts/corsia.d/service.sh
service_is_loaded() { return 0; }
launchctl() { printf 'launchctl %s\n' "$*" >> "$TRACE"; }
render_service_plist() { printf 'rendered\n' >> "$TRACE"; }
"""
        + operation,
        env={"AGENTS_DIR": str(agents_dir), "TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert plist.read_bytes() == original
    assert not trace.exists()
    assert expected_message in result.stderr


@pytest.mark.parametrize(
    ("unset_variable", "expected_message"),
    [
        ("PYTHON", "interpreter"),
        ("BACKEND_DIR", "backend directory"),
        ("BACKEND_LOG", "log path"),
        ("BACKEND_PORT", "numeric backend port"),
        ("CORSIA_WORKSPACE", "workspace root"),
        ("CORSIA_STATE_DIR", "state root"),
    ],
)
def test_render_refuses_an_unresolved_service_environment(
    tmp_path: Path,
    unset_variable: str,
    expected_message: str,
) -> None:
    trace = tmp_path / "render.log"
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL:%s\\n' "$*" >> "$TRACE"; }
BACKEND_PORT=9999
BACKEND_DIR=/tmp/corsia/backend
BACKEND_LOG=/tmp/corsia/backend.log
PYTHON=/usr/bin/python3
CORSIA_WORKSPACE=/tmp/corsia/workspace
CORSIA_STATE_DIR=/tmp/corsia/state
ENV_FILE=/tmp/corsia-no-such-env
source scripts/corsia.d/service.sh
unset "$UNSET_VARIABLE"
render_service_plist generation-test
""",
        env={"TRACE": str(trace), "UNSET_VARIABLE": unset_variable},
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert expected_message in trace.read_text()


def test_install_keeps_the_live_plist_when_the_environment_is_unresolved(
    isolated_launch_agents_dir: Path,
) -> None:
    """An unresolved caller must never replace a startable service definition.

    A CI or developer shell that sources the service script without the
    launcher's resolved paths used to install a plist with an empty program
    path, which launchd accepts and cannot start.
    """
    plist = isolated_launch_agents_dir / "com.corsia.prod.plist"
    plist.write_text("installed plist")

    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
BACKEND_PORT=9999
CORSIA_TOKEN=test-token
FORCE=false
source scripts/corsia.d/service.sh
service_is_loaded() { return 0; }
drain_and_wait() { return 0; }
launchctl() { printf 'launchctl %s\\n' "$*" >&2; }
service_restart
"""
    )

    assert result.returncode != 0
    assert plist.read_text() == "installed plist"
    assert "launchctl bootout" not in result.stderr


@pytest.mark.parametrize("failure_stage", ["render", "lint"])
def test_service_install_failure_preserves_installed_plist(
    tmp_path: Path,
    failure_stage: str,
) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    plist = agents_dir / "com.corsia.prod.plist"
    plist.write_text("installed plist")
    plist.chmod(0o600)

    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
LAUNCH_AGENTS_DIR="$AGENTS_DIR"
source scripts/corsia.d/service.sh
render_service_plist() {
    printf '%s' 'incomplete replacement'
    [ "$FAILURE_STAGE" != render ]
}
plutil() { [ "$FAILURE_STAGE" != lint ]; }
service_install
""",
        env={
            "AGENTS_DIR": str(agents_dir),
            "FAILURE_STAGE": failure_stage,
        },
    )

    assert result.returncode != 0
    assert plist.read_text() == "installed plist"
    assert stat.S_IMODE(plist.stat().st_mode) == 0o600


# --- Sanctioned self-redeploy (runtime.sh redeploy_self) --------------------

_REDEPLOY_PREAMBLE = """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL %s\\n' "$*" >> "$TRACE"; }
SELF_CHECKOUT="${SELF_CHECKOUT:-/tmp/corsia-serving}"
FORCE=false
source scripts/corsia.d/runtime.sh
acquire_fd_mutex() { return 0; }
is_backend_running() { return 0; }
drain_and_wait() { printf 'STEP:drain\\n' >> "$TRACE"; }
bounce_server() { printf 'STEP:bounce\\n' >> "$TRACE"; }
handoff_redeploy_restart() {
    printf 'STEP:handoff\\n' >> "$TRACE"
    drain_and_wait
    bounce_server
}
wait_for_backend_health() { return 0; }
redeploy_sync_python_dependencies() { printf 'STEP:dependencies\\n' >> "$TRACE"; return "${DEPS_RC:-0}"; }
redeploy_build() { printf 'STEP:build\\n' >> "$TRACE"; return "${BUILD_RC:-0}"; }
"""


def _run_redeploy(
    git_body: str,
    trace: Path,
    *,
    build_rc: str = "0",
    deps_rc: str = "0",
    pull_rc: str = "0",
):
    git_dir = trace.parent / "git-dir"
    git_dir.mkdir()
    checkout = trace.parent / "serving-checkout"
    checkout.mkdir()
    return _run_bash(
        _REDEPLOY_PREAMBLE + f"\ngit() {{ {git_body} }}\nredeploy_self\n",
        env={
            "TRACE": str(trace),
            "GIT_DIR": str(git_dir),
            "SELF_CHECKOUT": str(checkout),
            "BUILD_RC": build_rc,
            "DEPS_RC": deps_rc,
            "PULL_RC": pull_rc,
        },
    )


# Distinct STEP: markers so a refusal MESSAGE (which may contain words like
# "pull" or "build") never false-matches a step assertion.
_GIT_CLEAN_ON_BRANCH = """
case "$*" in
    *"rev-parse --is-inside-work-tree"*) echo true ;;
    *"rev-parse --absolute-git-dir"*) echo "$GIT_DIR" ;;
    *"symbolic-ref"*) echo dev ;;
    *"status --porcelain"*) : ;;
    *"pull --ff-only"*) printf 'STEP:pull\\n' >> "$TRACE"; return "${PULL_RC:-0}" ;;
    *) : ;;
esac
"""


def test_redeploy_pulls_builds_then_bounces_in_order(tmp_path: Path) -> None:
    trace = tmp_path / "t.log"
    result = _run_redeploy(_GIT_CLEAN_ON_BRANCH, trace)
    assert result.returncode == 0, trace.read_text() if trace.exists() else result.stderr
    body = trace.read_text()
    # Pull BEFORE dependency check BEFORE build BEFORE the updated-runtime
    # handoff; the handoff owns drain and bounce.
    assert body.index("STEP:pull") < body.index("STEP:dependencies")
    assert body.index("STEP:dependencies") < body.index("STEP:build")
    assert body.index("STEP:build") < body.index("STEP:handoff")
    assert body.index("STEP:handoff") < body.index("STEP:drain")
    assert body.index("STEP:drain") < body.index("STEP:bounce")
    assert "FAIL" not in body


@pytest.mark.parametrize(
    ("force", "expected"),
    [
        ("false", "restart-prepared"),
        ("true", "restart-prepared --force"),
    ],
)
def test_redeploy_handoff_executes_updated_checkout_script(
    tmp_path: Path, force: str, expected: str
) -> None:
    checkout = tmp_path / "checkout"
    script = checkout / "scripts" / "corsia"
    script.parent.mkdir(parents=True)
    script.write_text(
        "#!/bin/bash\n"
        "printf '%s\\n' \"$*\" > \"$TRACE\"\n"
    )
    script.chmod(0o755)
    trace = tmp_path / "handoff.log"

    result = _run_bash(
        """
fail() { printf 'FAIL %s\\n' "$*" >&2; }
source scripts/corsia.d/runtime.sh
FORCE="$TEST_FORCE"
handoff_redeploy_restart "$CHECKOUT"
""",
        env={
            "CHECKOUT": str(checkout),
            "TRACE": str(trace),
            "TEST_FORCE": force,
        },
    )

    assert result.returncode == 0, result.stderr
    assert trace.read_text().strip() == expected


def test_redeploy_refuses_dirty_checkout(tmp_path: Path) -> None:
    trace = tmp_path / "t.log"
    result = _run_redeploy(
        """
case "$*" in
    *"rev-parse --is-inside-work-tree"*) echo true ;;
    *"rev-parse --absolute-git-dir"*) echo "$GIT_DIR" ;;
    *"symbolic-ref"*) echo dev ;;
    *"status --porcelain"*) echo " M backend/x.py" ;;
    *"pull --ff-only"*) printf 'STEP:pull\\n' >> "$TRACE" ;;
esac
""",
        trace,
    )
    assert result.returncode != 0
    body = trace.read_text()
    assert "FAIL" in body
    assert "STEP:" not in body  # nothing ran: no pull, build, or bounce

def test_redeploy_refuses_detached_head(tmp_path: Path) -> None:
    trace = tmp_path / "t.log"
    result = _run_redeploy(
        """
case "$*" in
    *"rev-parse --is-inside-work-tree"*) echo true ;;
    *"symbolic-ref"*) return 1 ;;
    *) : ;;
esac
""",
        trace,
    )
    assert result.returncode != 0
    body = trace.read_text()
    assert "FAIL" in body
    assert "STEP:" not in body


def test_redeploy_refuses_non_git_checkout(tmp_path: Path) -> None:
    trace = tmp_path / "t.log"
    result = _run_redeploy('case "$*" in *"rev-parse --is-inside-work-tree"*) return 1 ;; esac', trace)
    assert result.returncode != 0
    body = trace.read_text()
    assert "FAIL" in body and "STEP:" not in body


def test_redeploy_does_not_bounce_after_pull_failure(tmp_path: Path) -> None:
    trace = tmp_path / "t.log"
    result = _run_redeploy(_GIT_CLEAN_ON_BRANCH, trace, pull_rc="1")
    assert result.returncode != 0
    body = trace.read_text()
    assert "STEP:pull" in body  # it attempted the pull
    assert "STEP:build" not in body and "STEP:bounce" not in body  # ...and stopped


def test_redeploy_does_not_bounce_after_build_failure(tmp_path: Path) -> None:
    trace = tmp_path / "t.log"
    result = _run_redeploy(_GIT_CLEAN_ON_BRANCH, trace, build_rc="1")
    assert result.returncode != 0
    body = trace.read_text()
    assert "STEP:build" in body  # build ran (after a good pull + dependency check)
    assert "STEP:bounce" not in body  # ...and the instance was NOT taken down


def test_redeploy_does_not_build_or_bounce_after_dependency_failure(tmp_path: Path) -> None:
    trace = tmp_path / "t.log"
    result = _run_redeploy(_GIT_CLEAN_ON_BRANCH, trace, deps_rc="1")
    assert result.returncode != 0
    body = trace.read_text()
    assert "STEP:pull" in body
    assert "STEP:dependencies" in body
    assert "STEP:build" not in body and "STEP:bounce" not in body


def test_redeploy_sync_python_dependencies_uses_the_configured_python(tmp_path: Path) -> None:
    trace = tmp_path / "python.log"
    result = _run_bash(
        """
fail() { :; }
PYTHON=record_python
source scripts/corsia.d/runtime.sh
record_python() { printf '%s\\n' "$*" >> "$TRACE"; }
redeploy_sync_python_dependencies /srv/checkout
""",
        env={"TRACE": str(trace)},
    )
    assert result.returncode == 0
    assert trace.read_text().splitlines() == [
        "-m pip install -r /srv/checkout/backend/requirements.lock",
        "-m pip check",
    ]


def test_redeploy_refuses_when_locked_python_install_fails(tmp_path: Path) -> None:
    trace = tmp_path / "python.log"
    result = _run_bash(
        """
fail() { printf 'FAIL:%s\\n' "$*" >> "$TRACE"; }
PYTHON=record_python
source scripts/corsia.d/runtime.sh
record_python() { printf '%s\\n' "$*" >> "$TRACE"; [ "$3" != install ]; }
redeploy_sync_python_dependencies /srv/checkout
""",
        env={"TRACE": str(trace)},
    )
    assert result.returncode != 0
    body = trace.read_text()
    assert "Redeploy refused: pip install" in body
    assert "pip check" not in body


def test_redeploy_installs_frontend_dependencies_when_node_modules_exists(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    frontend = checkout / "frontend"
    (frontend / "node_modules").mkdir(parents=True)
    trace = tmp_path / "npm.log"

    result = _run_bash(
        """
fail() { :; }
source scripts/corsia.d/runtime.sh
npm() {
    printf '%s\\n' "$*" >> "$TRACE"
    [ "$1" != run ] || mkdir -p "$CORSIA_BUILD_OUTPUT"
}
redeploy_build "$CHECKOUT"
""",
        env={"CHECKOUT": str(checkout), "TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == [
        "install --no-audit --no-fund --no-save",
        "run build",
    ]


def test_redeploy_refuses_and_skips_build_when_npm_install_fails(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    frontend = checkout / "frontend"
    (frontend / "node_modules").mkdir(parents=True)
    trace = tmp_path / "npm.log"

    result = _run_bash(
        """
fail() { printf 'FAIL:%s\\n' "$*" >> "$TRACE"; }
source scripts/corsia.d/runtime.sh
npm() { printf 'npm:%s\\n' "$1" >> "$TRACE"; [ "$1" != install ]; }
redeploy_build "$CHECKOUT"
""",
        env={"CHECKOUT": str(checkout), "TRACE": str(trace)},
    )

    assert result.returncode != 0
    body = trace.read_text()
    assert "Redeploy refused: npm install failed" in body
    assert "npm:run" not in body
    assert not (frontend / "node_modules" / ".corsia-dist-stage").exists()


def _run_staged_frontend_build(checkout: Path, *, build_rc: str, body: str):
    return _run_bash(
        """
fail() { :; }
source scripts/corsia.d/runtime.sh
npm() {
    [ "$1" = run ] || return 0
    mkdir -p "$CORSIA_BUILD_OUTPUT"
    printf '%s' "$NEW_BUILD" > "$CORSIA_BUILD_OUTPUT/index.html"
    return "$BUILD_RC"
}
redeploy_build "$CHECKOUT"
""",
        env={
            "BUILD_RC": build_rc,
            "CHECKOUT": str(checkout),
            "NEW_BUILD": body,
        },
    )

def test_failed_frontend_build_preserves_live_assets(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    frontend = checkout / "frontend"
    (frontend / "node_modules").mkdir(parents=True)
    (frontend / "dist").mkdir()
    live_asset = frontend / "dist" / "index.html"
    live_asset.write_text("old build")

    result = _run_staged_frontend_build(checkout, build_rc="1", body="partial build")

    assert result.returncode != 0
    assert live_asset.read_text() == "old build"
    assert not (frontend / "node_modules" / ".corsia-dist-stage").exists()


def test_successful_frontend_build_stays_staged_until_safe_restart(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    frontend = checkout / "frontend"
    (frontend / "node_modules").mkdir(parents=True)
    (frontend / "dist").mkdir()
    live_asset = frontend / "dist" / "index.html"
    live_asset.write_text("old build")

    result = _run_staged_frontend_build(checkout, build_rc="0", body="new build")

    stage = frontend / "node_modules" / ".corsia-dist-stage"
    assert result.returncode == 0
    assert live_asset.read_text() == "old build"
    assert (stage / "index.html").read_text() == "new build"
    assert Path(f"{stage}.prepared").exists()


def _run_prepared_restart(
    checkout: Path,
    trace: Path,
    *,
    drain_rc: str = "0",
    bounce_rc: str = "0",
    backend_rc: str = "0",
):
    stage = checkout / "frontend" / "node_modules" / ".corsia-dist-stage"
    if stage.exists():
        Path(f"{stage}.prepared").touch()
    return _run_bash(
        """
fail() { printf 'FAIL:%s\\n' "$*" >> "$TRACE"; }
warn() { :; }
source scripts/corsia.d/runtime.sh
SELF_CHECKOUT="$CHECKOUT"
FORCE=false
is_backend_running() { return "$BACKEND_RC"; }
drain_and_wait() { printf 'STEP:drain\\n' >> "$TRACE"; return "$DRAIN_RC"; }
bounce_server() {
    printf 'STEP:bounce\\n' >> "$TRACE"
    [ "$BOUNCE_RC" = "0" ] || return "$BOUNCE_RC"
    [ -z "${1:-}" ] || "$1"
}
wait_for_backend_health() { printf 'STEP:health\\n' >> "$TRACE"; }
cancel_failed_drain() { printf 'STEP:cancel-drain\\n' >> "$TRACE"; }
restart_prepared
""",
        env={
            "BACKEND_RC": backend_rc,
            "BOUNCE_RC": bounce_rc,
            "CHECKOUT": str(checkout),
            "DRAIN_RC": drain_rc,
            "TRACE": str(trace),
        },
    )


def test_unhealthy_backend_retains_prepared_release_before_drain(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    frontend = checkout / "frontend"
    stage = frontend / "node_modules" / ".corsia-dist-stage"
    (frontend / "dist").mkdir(parents=True)
    stage.mkdir(parents=True)
    (frontend / "dist" / "index.html").write_text("old build")
    (stage / "index.html").write_text("new build")
    trace = tmp_path / "trace.log"

    result = _run_prepared_restart(checkout, trace, backend_rc="1")

    assert result.returncode != 0
    assert (frontend / "dist" / "index.html").read_text() == "old build"
    assert (stage / "index.html").read_text() == "new build"
    assert Path(f"{stage}.prepared").exists()
    assert "STEP:drain" not in trace.read_text()
    assert "drain ownership cannot be proven" in trace.read_text()


def test_failed_drain_still_completes_the_update(
    tmp_path: Path,
) -> None:
    """Update is the only path that pulls, so refusing here strands the box.

    This used to retain the stage and return failure, which meant a machine
    holding work that would not yield could never receive new code again. The
    only escape was --force, which skips the drain entirely and kills that work
    with no recovery registration -- strictly worse than draining to the hard
    cap and proceeding.
    """
    checkout = tmp_path / "checkout"
    frontend = checkout / "frontend"
    stage = frontend / "node_modules" / ".corsia-dist-stage"
    (frontend / "dist").mkdir(parents=True)
    stage.mkdir(parents=True)
    (frontend / "dist" / "index.html").write_text("old build")
    (stage / "index.html").write_text("new build")
    trace = tmp_path / "trace.log"

    result = _run_prepared_restart(checkout, trace, drain_rc="1")

    assert result.returncode == 0
    assert (frontend / "dist" / "index.html").read_text() == "new build"
    assert not Path(f"{stage}.prepared").exists()
    assert trace.read_text().splitlines() == [
        "STEP:drain",
        "STEP:bounce",
    ]


def test_update_releases_the_hold_when_it_fails_before_teardown(
    tmp_path: Path,
) -> None:
    """No bounce is coming to settle the operation, so ingress must be freed."""
    checkout = tmp_path / "checkout"
    frontend = checkout / "frontend"
    stage = frontend / "node_modules" / ".corsia-dist-stage"
    (frontend / "dist").mkdir(parents=True)
    stage.mkdir(parents=True)
    (frontend / "dist" / "index.html").write_text("old build")
    (stage / "index.html").write_text("new build")
    trace = tmp_path / "trace.log"

    result = _run_prepared_restart(
        checkout, trace, drain_rc="1", bounce_rc="1",
    )

    assert result.returncode != 0
    assert (frontend / "dist" / "index.html").read_text() == "old build"
    assert "STEP:cancel-drain" in trace.read_text().splitlines()


def test_update_holds_the_drain_it_could_not_complete(
    tmp_path: Path,
) -> None:
    """Releasing mid-update would readmit work into the gap before teardown."""
    checkout = tmp_path / "checkout"
    frontend = checkout / "frontend"
    stage = frontend / "node_modules" / ".corsia-dist-stage"
    (frontend / "dist").mkdir(parents=True)
    stage.mkdir(parents=True)
    (frontend / "dist" / "index.html").write_text("old build")
    (stage / "index.html").write_text("new build")
    Path(f"{stage}.prepared").touch()
    trace = tmp_path / "trace.log"

    result = _run_bash(
        """
fail() { :; }
warn() { :; }
source scripts/corsia.d/runtime.sh
SELF_CHECKOUT="$CHECKOUT"
FORCE=false
is_backend_running() { return 0; }
drain_and_wait() {
    printf 'failure-action=%s\\n' "${DRAIN_FAILURE_ACTION:-release}" >> "$TRACE"
    return 1
}
bounce_server() { [ -z "${1:-}" ] || "$1"; }
cancel_failed_drain() { :; }
restart_prepared
""",
        env={"CHECKOUT": str(checkout), "TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert "failure-action=hold" in trace.read_text()


def test_failed_bounce_retains_unactivated_release_and_prior_build(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    frontend = checkout / "frontend"
    stage = frontend / "node_modules" / ".corsia-dist-stage"
    (frontend / "dist").mkdir(parents=True)
    stage.mkdir(parents=True)
    (frontend / "dist" / "index.html").write_text("old build")
    (stage / "index.html").write_text("new build")
    trace = tmp_path / "trace.log"

    result = _run_prepared_restart(checkout, trace, bounce_rc="1")

    assert result.returncode != 0
    assert (frontend / "dist" / "index.html").read_text() == "old build"
    assert (stage / "index.html").read_text() == "new build"
    assert Path(f"{stage}.prepared").exists()
    assert trace.read_text().index("STEP:drain") < trace.read_text().index("STEP:bounce")


def test_prepared_frontend_is_not_exposed_before_confirmed_teardown(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    frontend = checkout / "frontend"
    stage = frontend / "node_modules" / ".corsia-dist-stage"
    (frontend / "dist").mkdir(parents=True)
    stage.mkdir(parents=True)
    (frontend / "dist" / "index.html").write_text("old build")
    (stage / "index.html").write_text("new build")
    Path(f"{stage}.prepared").touch()
    trace = tmp_path / "trace.log"

    result = _run_bash(
        """
fail() { :; }
warn() { :; }
source scripts/corsia.d/runtime.sh
SELF_CHECKOUT="$CHECKOUT"
FORCE=false
is_backend_running() { return 0; }
drain_and_wait() { return 0; }
bounce_server() {
    printf 'ASSET_AT_TEARDOWN:%s\n' \
        "$(cat "$CHECKOUT/frontend/dist/index.html")" >> "$TRACE"
    return 1
}
cancel_failed_drain() { :; }
restart_prepared
""",
        env={"CHECKOUT": str(checkout), "TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert trace.read_text().splitlines() == ["ASSET_AT_TEARDOWN:old build"]


def test_failed_post_teardown_activation_preserves_both_recovery_builds(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    frontend = checkout / "frontend"
    stage = frontend / "node_modules" / ".corsia-dist-stage"
    backup = frontend / "node_modules" / ".corsia-dist-previous"
    (frontend / "dist").mkdir(parents=True)
    stage.mkdir(parents=True)
    (frontend / "dist" / "index.html").write_text("old build")
    (stage / "index.html").write_text("new build")
    Path(f"{stage}.prepared").touch()
    trace = tmp_path / "trace.log"

    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL:%s\\n' "$*" >> "$TRACE"; }
source scripts/corsia.d/runtime.sh
SELF_CHECKOUT="$CHECKOUT"
FORCE=false
is_backend_running() { return 0; }
drain_and_wait() { return 0; }
bounce_server() { "$1"; }
mv() {
    [ "$1" != "$STAGE" ] || return 1
    command mv "$@"
}
cancel_failed_drain() { :; }
restart_prepared
""",
        env={
            "CHECKOUT": str(checkout),
            "STAGE": str(stage),
            "TRACE": str(trace),
        },
    )

    assert result.returncode != 0
    assert not (frontend / "dist").exists()
    assert (stage / "index.html").read_text() == "new build"
    assert (backup / "index.html").read_text() == "old build"
    failure = trace.read_text()
    assert str(stage) in failure
    assert str(backup) in failure


def test_successful_restart_activates_staged_frontend_after_drain(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    frontend = checkout / "frontend"
    stage = frontend / "node_modules" / ".corsia-dist-stage"
    (frontend / "dist").mkdir(parents=True)
    stage.mkdir(parents=True)
    (frontend / "dist" / "index.html").write_text("old build")
    (stage / "index.html").write_text("new build")
    trace = tmp_path / "trace.log"

    result = _run_prepared_restart(checkout, trace)

    assert result.returncode == 0
    assert (frontend / "dist" / "index.html").read_text() == "new build"
    assert not stage.exists()
    assert not Path(f"{stage}.prepared").exists()
    assert trace.read_text().splitlines() == [
        "STEP:drain",
        "STEP:bounce",
    ]


def test_bounce_server_stops_when_backend_stop_fails(tmp_path: Path) -> None:
    trace = tmp_path / "trace.log"
    result = _run_bash(
        """
warn() { :; }
fail() { :; }
source scripts/corsia.d/runtime.sh
resolve_bind_host() { echo 0.0.0.0; }
FORCE=true
service_managed() { return 1; }
stop_backend() { printf 'STEP:stop\\n' >> "$TRACE"; return 1; }
start_backend() { printf 'STEP:start\\n' >> "$TRACE"; }
bounce_server
""",
        env={"TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert trace.read_text().splitlines() == ["STEP:stop"]


_STOP_OWNERSHIP_PREAMBLE = """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
BACKEND_DIR="$MINE"
# stop_backend compares directories through the helper in service.sh, so load
# the pair the CLI loads; a missing function would refuse for the wrong reason.
source scripts/corsia.d/runtime.sh
source scripts/corsia.d/service.sh
backend_pids() { printf '123\\n'; }
kill() { printf 'kill %s\\n' "$*" >> "$TRACE"; [ "${1:-}" = "-0" ] && return 1; return 0; }
sleep() { :; }
"""


def test_stop_backend_refuses_a_listener_from_another_checkout(
    tmp_path: Path,
) -> None:
    """Killing by port is killing whoever answers; the owner has to be proved.

    A worktree and production differ only by which directory their backend runs
    from, so the process working directory is what says whose instance this is.
    """
    trace = tmp_path / "kill.log"
    mine = tmp_path / "mine" / "backend"
    other = tmp_path / "other" / "backend"
    mine.mkdir(parents=True)
    other.mkdir(parents=True)

    result = _run_bash(
        _STOP_OWNERSHIP_PREAMBLE
        + """
backend_pid_working_directory() { printf '%s\\n' "$OTHER"; }
stop_backend
""",
        env={"MINE": str(mine), "OTHER": str(other), "TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert not trace.exists()
    assert "another checkout" in result.stderr


def test_stop_backend_stops_a_listener_running_from_this_checkout(
    tmp_path: Path,
) -> None:
    """The positive control for the guard above: our own backend still stops.

    Without this, a refusal proves nothing — an unbound helper would refuse
    every listener just as convincingly as a real directory comparison.
    """
    trace = tmp_path / "kill.log"
    mine = tmp_path / "mine" / "backend"
    mine.mkdir(parents=True)

    result = _run_bash(
        _STOP_OWNERSHIP_PREAMBLE
        + """
backend_pid_working_directory() { printf '%s\\n' "$MINE"; }
stop_backend
""",
        env={"MINE": str(mine), "TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert "kill 123" in trace.read_text()


def test_stop_backend_accepts_a_symlinked_spelling_of_this_checkout(
    tmp_path: Path,
) -> None:
    """Production's root is reached through a symlink; lsof reports the real path.

    Comparing the two spellings literally would refuse to stop the very backend
    the operator is standing in.
    """
    trace = tmp_path / "kill.log"
    real = tmp_path / "real" / "backend"
    real.mkdir(parents=True)
    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path / "real", target_is_directory=True)

    result = _run_bash(
        _STOP_OWNERSHIP_PREAMBLE
        + """
backend_pid_working_directory() { printf '%s\\n' "$REAL"; }
stop_backend
""",
        env={
            "MINE": str(linked / "backend"),
            "REAL": str(real),
            "TRACE": str(trace),
        },
    )

    assert result.returncode == 0, result.stderr
    assert "kill 123" in trace.read_text()


def test_direct_lifecycle_refuses_a_listener_it_cannot_attribute(
    tmp_path: Path,
) -> None:
    """An unreadable working directory is not permission to act on the process."""
    trace = tmp_path / "kill.log"
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
BACKEND_PORT=9999
BACKEND_DIR=/mine/backend
source scripts/corsia.d/runtime.sh
service_is_loaded() { return 1; }
backend_pids() { printf '123\\n'; }
backend_pid_working_directory() { return 1; }
kill() { printf 'kill %s\\n' "$*" >> "$TRACE"; }
require_port_served_by_this_checkout
""",
        env={"TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert not trace.exists()
    assert "Cannot prove" in result.stderr


def test_port_guard_allows_start_when_own_port_is_free_despite_loaded_service(
    tmp_path: Path,
) -> None:
    """A same-label launchd job loaded on a DIFFERENT port must not block a
    free checkout port. require_port_served_by_this_checkout must check
    whether THIS checkout's port has any listener before consulting the
    loaded service at all -- otherwise an isolated worktree with its own
    BACKEND_PORT can never start while the unrelated prod job is loaded,
    which is the ordinary state of the world."""
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
BACKEND_PORT=3001
BACKEND_DIR=/mine/backend
source scripts/corsia.d/runtime.sh
source scripts/corsia.d/service.sh
service_is_loaded() { return 0; }
service_loaded_pid() { printf '111\\n'; }
backend_pids() { printf ''; }
require_port_served_by_this_checkout
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_port_guard_still_refuses_when_loaded_service_owns_this_port(
    tmp_path: Path,
) -> None:
    """The prior fix must not weaken the case it exists for: a loaded
    launchd job that actually owns this checkout's own port still refuses
    direct management, since killing it would only have KeepAlive revive it."""
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
BACKEND_PORT=""" + str(DEFAULT_BACKEND_PORT) + """
BACKEND_DIR=/mine/backend
source scripts/corsia.d/runtime.sh
source scripts/corsia.d/service.sh
service_is_loaded() { return 0; }
service_loaded_pid() { printf '111\\n'; }
backend_pids() { printf '111\\n'; }
require_port_served_by_this_checkout
"""
    )

    assert result.returncode != 0
    assert "refusing to replace an unverified job" in result.stderr.lower()


def test_stop_backend_reports_process_that_survives_force_kill() -> None:
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
source scripts/corsia.d/runtime.sh
backend_pids() { printf '123\\n'; }
backend_pid_serves_this_checkout() { return 0; }
kill() { [ "${1:-}" = "-0" ] && return 0; return 1; }
sleep() { :; }
stop_backend
"""
    )

    assert result.returncode != 0


def test_stop_backend_succeeds_when_process_exits_before_force_kill() -> None:
    # SIGKILL returns non-zero when the process already exited during the TERM
    # wait. Liveness, not kill's exit code, decides success.
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
source scripts/corsia.d/runtime.sh
backend_pids() { printf '123\\n'; }
backend_pid_serves_this_checkout() { return 0; }
kill() {
    case "${1:-}" in
        -0) return 1 ;;   # process is gone
        -9) return 1 ;;   # kill reports "no such process"
        *)  return 0 ;;
    esac
}
sleep() { :; }
stop_backend
"""
    )

    assert result.returncode == 0


def test_post_teardown_failure_without_frontend_reports_that_exact_state(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    trace = tmp_path / "trace.log"

    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL:%s\\n' "$*" >> "$TRACE"; }
source scripts/corsia.d/runtime.sh
SELF_CHECKOUT="$CHECKOUT"
FORCE=false
is_backend_running() { return 0; }
drain_and_wait() { return 0; }
bounce_server() {
    "$1"
    return 1
}
cancel_failed_drain() { :; }
restart_prepared
""",
        env={"CHECKOUT": str(checkout), "TRACE": str(trace)},
    )

    assert result.returncode != 0
    failure = trace.read_text()
    assert "no frontend assets were present" in failure
    assert "prepared frontend remains paired" not in failure


def test_post_teardown_failure_keeps_the_updated_release_paired(
    tmp_path: Path,
) -> None:
    # Once the old backend is gone, restoring the old frontend would pair it with
    # the already-updated on-disk backend. Keep the new release authoritative.
    checkout = tmp_path / "checkout"
    frontend = checkout / "frontend"
    stage = frontend / "node_modules" / ".corsia-dist-stage"
    (frontend / "dist").mkdir(parents=True)
    stage.mkdir(parents=True)
    (frontend / "dist" / "index.html").write_text("old build")
    (stage / "index.html").write_text("new build")
    Path(f"{stage}.prepared").touch()

    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL:%s\\n' "$*" >> "$TRACE"; }
source scripts/corsia.d/runtime.sh
SELF_CHECKOUT="$CHECKOUT"
FORCE=false
is_backend_running() { return 0; }
drain_and_wait() { return 0; }
bounce_server() {
    "$1"
    return 1
}
cancel_failed_drain() { :; }
restart_prepared
""",
        env={"CHECKOUT": str(checkout), "TRACE": str(tmp_path / "trace.log")},
    )

    assert result.returncode != 0
    assert not stage.exists()
    assert (frontend / "dist" / "index.html").read_text() == "new build"
    assert not (frontend / "node_modules" / ".corsia-dist-previous").exists()
    failure = (tmp_path / "trace.log").read_text()
    assert "prepared frontend remains paired with the updated on-disk backend" in failure


def test_prepare_refuses_leftover_state_with_recovery_guidance(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    frontend = checkout / "frontend"
    (frontend / "node_modules" / ".corsia-dist-previous").mkdir(parents=True)

    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL:%s\\n' "$*" >> "$TRACE"; }
source scripts/corsia.d/runtime.sh
npm() { :; }
redeploy_build "$CHECKOUT"
""",
        env={"CHECKOUT": str(checkout), "TRACE": str(tmp_path / "trace.log")},
    )

    assert result.returncode != 0
    failure = (tmp_path / "trace.log").read_text()
    assert ".corsia-dist-previous" in failure
    assert "before changing that recovery state" in failure


def test_outer_lifecycle_envelope_disables_nested_runtime_cleanup(tmp_path: Path) -> None:
    trace = tmp_path / "cleanup"
    result = _run_bash(
        """
source scripts/corsia.d/runtime.sh
readonly _CORSIA_LIFECYCLE_ENVELOPE_ACTIVE=true
CORSIA_RESTART_OPERATION_ID=abababababababababababababababab
cancel_drain_if_confirmed() { touch "$CLEANUP_TRACE"; }
fail_late() { return 17; }
run_owned_restart_lifecycle fail_late
""",
        env={"CLEANUP_TRACE": str(trace)},
    )

    assert result.returncode == 17
    assert not trace.exists()


def test_owned_redeploy_sources_updated_script_in_same_shell_with_original_tuple(
    tmp_path: Path,
) -> None:
    """Updated lifecycle loading keeps the outer trap and immutable request tuple."""
    source_script = Path(__file__).resolve().parents[2] / "scripts" / "corsia"
    source_lines = source_script.read_text().splitlines()
    prefix_end = next(
        index for index, line in enumerate(source_lines, start=1)
        if line.startswith("RED=")
    )
    prefix = "\n".join(source_lines[:prefix_end])
    checkout = tmp_path / "checkout"
    home = tmp_path / "home"
    home.mkdir()
    updated_script = checkout / "scripts" / "corsia"
    updated_script.parent.mkdir(parents=True)
    # The prefix sources account helpers and validates its own backend roots, so
    # the updated checkout carries the same backend shape as a real redeploy.
    helpers = updated_script.parent / "corsia.d"
    helpers.mkdir()
    accounts_helper = source_script.parent / "corsia.d" / "accounts.sh"
    (helpers / "accounts.sh").write_text(accounts_helper.read_text())
    (checkout / "backend").symlink_to(
        source_script.parents[1] / "backend", target_is_directory=True
    )
    updated_script.write_text(
        prefix
        + "\n"
        + "printf '%s|%s|%s|%s\\n' \"$$\" "
        + "\"$_CORSIA_REQUEST_BACKEND_PORT\" "
        + "\"$_CORSIA_REQUEST_OPERATION_ID\" "
        + "\"$CORSIA_TOKEN\" > \"$UPDATED_TRACE\"\n"
        + "env > \"$UPDATED_ENV\"\n"
    )
    updated_script.chmod(0o755)
    before = tmp_path / "before"
    after = tmp_path / "after"
    updated = tmp_path / "updated"
    updated_env = tmp_path / "updated-env"
    env_file = tmp_path / "corsia.env"
    env_file.write_text("export CORSIA_TOKEN=stale-exported-token\n")

    result = _run_bash(
        """
source scripts/corsia.d/runtime.sh
FORCE=false
readonly _CORSIA_REQUEST_BACKEND_PORT=4113
readonly _CORSIA_REQUEST_OPERATION_ID=abababababababababababababababab
readonly _CORSIA_REQUEST_CALLER_TOKEN=original-credential
readonly _CORSIA_LIFECYCLE_ENVELOPE_ACTIVE=true
printf '%s' "$$" > "$BEFORE_TRACE"
handoff_redeploy_restart "$CHECKOUT"
printf '%s' "$$" > "$AFTER_TRACE"
""",
        env={
            "AFTER_TRACE": str(after),
            "BEFORE_TRACE": str(before),
            "CHECKOUT": str(checkout),
            "HOME": str(home),
            "CORSIA_ENV_FILE": str(env_file),
            "CORSIA_PYTHON": sys.executable,
            "CORSIA_RESTART_BACKEND_PORT": "4113",
            "CORSIA_RESTART_OPERATION_ID": "abababababababababababababababab",
            "CORSIA_STATE_DIR": str(tmp_path / "state"),
            "CORSIA_WORKSPACE": str(tmp_path / "workspace"),
            "UPDATED_ENV": str(updated_env),
            "UPDATED_TRACE": str(updated),
        },
    )

    assert result.returncode == 0, result.stderr
    assert after.read_text() == before.read_text()
    assert updated.read_text().strip() == (
        f"{before.read_text()}|4113|abababababababababababababababab|"
        "original-credential"
    )
    assert "CORSIA_TOKEN=" not in updated_env.read_text()
    assert "original-credential" not in updated_env.read_text()


def test_stop_releases_the_hold_when_teardown_fails(tmp_path: Path) -> None:
    """A failed teardown after a failed drain must not freeze ingress forever.

    The old backend is still serving and no restart is coming to settle the
    operation at startup, so a hold left on here lasts the life of that
    process. The release used to be gated on the drain having *completed*,
    which is exactly the case this path cannot satisfy.
    """
    trace = tmp_path / "launchctl.log"
    result = _run_bash(
        _SERVICE_STOP_PREAMBLE
        + """
drain_and_wait() { return 1; }
bootout_service_and_wait() { return 1; }
cancel_failed_drain() { printf 'cancel-drain\\n' >> "$LAUNCHCTL_TRACE"; }
service_stop
""",
        env={"LAUNCHCTL_TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert "cancel-drain" in trace.read_text().splitlines()


def test_restart_releases_the_hold_when_teardown_fails(tmp_path: Path) -> None:
    """Same dead end on the managed restart path."""
    trace = tmp_path / "launchctl.log"
    result = _run_bash(
        _SERVICE_RESTART_PREAMBLE
        + """
drain_and_wait() { return 1; }
bootout_service_and_wait() { return 1; }
install_preserved_service_definition() { return 0; }
cancel_failed_drain() { printf 'cancel-drain\\n' >> "$LAUNCHCTL_TRACE"; }
launchctl() { printf '%s\\n' "$*" >> "$LAUNCHCTL_TRACE"; }
service_restart
""",
        env={"LAUNCHCTL_TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert "cancel-drain" in trace.read_text().splitlines()


def test_unmanaged_restart_releases_the_hold_when_stop_fails(
    tmp_path: Path,
) -> None:
    """The unmanaged path returned directly, never reaching any compensation."""
    trace = tmp_path / "trace.log"
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
BACKEND_PORT=9999
FORCE=false
source scripts/corsia.d/runtime.sh
resolve_bind_host() { echo 0.0.0.0; }
is_backend_running() { return 0; }
read_backend_start_iso() { printf 'start-iso'; }
service_managed() { return 1; }
service_installed() { return 1; }
drain_and_wait() { return 1; }
stop_backend() { return 1; }
cancel_failed_drain() { printf 'cancel-drain\\n' >> "$TRACE"; }
bounce_server
""",
        env={"TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert "cancel-drain" in trace.read_text().splitlines()


def test_unconfirmed_teardown_keeps_the_hold_and_says_so(tmp_path: Path) -> None:
    """Unconfirmed teardown may mean a dying backend, so holding is correct.

    It must not claim the drain finished either: the hold is retained whenever
    a drain was attempted, including one that timed out.
    """
    trace = tmp_path / "launchctl.log"
    result = _run_bash(
        _SERVICE_RESTART_PREAMBLE
        + """
drain_and_wait() { return 1; }
bootout_service_and_wait() { return 2; }
install_preserved_service_definition() { return 0; }
cancel_failed_drain() { printf 'cancel-drain\\n' >> "$LAUNCHCTL_TRACE"; }
launchctl() { printf '%s\\n' "$*" >> "$LAUNCHCTL_TRACE"; }
service_restart
""",
        env={"LAUNCHCTL_TRACE": str(trace)},
    )

    assert result.returncode != 0
    traced = trace.read_text().splitlines() if trace.exists() else []
    assert "cancel-drain" not in traced
    assert "ingress remains held" in result.stderr
    assert "ingress remains drained" not in result.stderr
