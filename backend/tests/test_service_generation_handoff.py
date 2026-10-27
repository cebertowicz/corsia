from __future__ import annotations

import sys
from pathlib import Path

from service_script_fixtures import (  # noqa: F401
    isolated_launch_agents_dir,
    run_bash as _run_bash,
)


_SERVICE_RESTART_PREAMBLE = """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
BACKEND_PORT=9999
CORSIA_TOKEN=[REDACTED]
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
service_definition_path() { printf '%s' "$SERVICE_PLIST"; }
drain_and_wait() { return 0; }
wait_for_backend_health() {
    printf 'health\\n' >> "$LAUNCHCTL_TRACE"
    return "${HEALTH_RC:-0}"
}
"""


def test_service_loaded_pid_parses_launchd_job_output() -> None:
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
source scripts/corsia.d/service.sh
service_domain() { printf 'gui/501'; }
service_label() { printf 'com.corsia.prod'; }
launchctl() {
    printf '%s\n' 'gui/501/com.corsia.prod = {' '    state = running' '    pid = 4242' '}'
}
service_loaded_pid
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "4242\n"


def test_service_loaded_pid_rejects_unparseable_output() -> None:
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
source scripts/corsia.d/service.sh
service_domain() { printf 'gui/501'; }
service_label() { printf 'com.corsia.prod'; }
launchctl() {
    printf '%s\n' 'gui/501/com.corsia.prod = {' '    pid = not-a-pid' '}'
}
service_loaded_pid
"""
    )

    assert result.returncode != 0
    assert result.stdout == ""


def test_launchctl_bootout_wait_is_bounded(tmp_path: Path) -> None:
    fake_launchctl = tmp_path / "launchctl"
    fake_launchctl.write_text("#!/bin/bash\nexec /bin/sleep 10\n")
    fake_launchctl.chmod(0o755)

    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
source scripts/corsia.d/service.sh
SERVICE_PID_EXIT_WAIT_SECONDS=1
launchctl_bootout_wait gui/501/com.corsia.prod
""",
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "PYTHON": sys.executable,
        },
    )

    assert result.returncode == 124


def test_bootout_waits_through_launchd_when_the_job_has_no_pid(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "launchctl.log"
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
source scripts/corsia.d/service.sh
launchctl_bootout_wait() { launchctl bootout --wait "$1"; }
service_loaded_pid() { printf 'PID snapshot must not be used' >&2; return 1; }
launchctl() { printf '%s\\n' "$*" >> "$TRACE"; }
bootout_service_and_wait gui/501 com.corsia.prod
""",
        env={"TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == [
        "bootout --wait gui/501/com.corsia.prod",
    ]


def test_bootout_timeout_blocks_replacement_without_reclassifying_state() -> None:
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
source scripts/corsia.d/service.sh
launchctl_bootout_wait() { return 124; }
service_is_loaded() { printf 'state must not be reclassified' >&2; return 0; }
bootout_service_and_wait gui/501 com.corsia.prod
"""
    )

    assert result.returncode == 2
    assert "did not complete after 125s" in result.stderr
    assert "state must not be reclassified" not in result.stderr


def test_bootout_failure_while_job_remains_loaded_is_pre_removal_failure() -> None:
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
source scripts/corsia.d/service.sh
launchctl_bootout_wait() { return 1; }
service_is_loaded() { return 0; }
bootout_service_and_wait gui/501 com.corsia.prod
"""
    )

    assert result.returncode == 1


def test_bootout_failure_after_job_disappears_blocks_replacement() -> None:
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { printf 'FAIL: %s\\n' "$*" >&2; }
source scripts/corsia.d/service.sh
launchctl_bootout_wait() { return 1; }
service_is_loaded() { return 1; }
bootout_service_and_wait gui/501 com.corsia.prod
"""
    )

    assert result.returncode == 2
    assert "did not confirm teardown completed" in result.stderr


def test_service_restart_never_bootstraps_without_confirmed_teardown(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "launchctl.log"
    result = _run_bash(
        _SERVICE_RESTART_PREAMBLE
        + """
cancel_failed_drain() { printf 'cancel-drain\\n' >> "$LAUNCHCTL_TRACE"; }
launchctl_bootout_wait() {
    printf 'bootout --wait %s\\n' "$1" >> "$LAUNCHCTL_TRACE"
    return 124
}
launchctl() { printf '%s\\n' "$*" >> "$LAUNCHCTL_TRACE"; }
service_restart
""",
        env={"LAUNCHCTL_TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert trace.read_text().splitlines() == [
        "bootout --wait gui/501/com.corsia.prod",
    ]
    assert Path(f"{trace}.plist").read_text() == "old plist\n"
    assert "did not complete after 125s" in result.stderr
    assert "ingress remains held" in result.stderr


def test_service_restart_does_not_restore_over_a_stale_generation(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "launchctl.log"
    result = _run_bash(
        _SERVICE_RESTART_PREAMBLE
        + """
: > "$LAUNCHCTL_TRACE"
service_is_loaded() {
    bootouts="$(grep -c '^bootout ' "$LAUNCHCTL_TRACE" || true)"
    [ "$bootouts" -lt 2 ]
}

cancel_failed_drain() { printf 'cancel-drain\\n' >> "$LAUNCHCTL_TRACE"; }
launchctl() {
    printf '%s\\n' "$*" >> "$LAUNCHCTL_TRACE"
    if [ "$1" = bootout ]; then
        bootouts="$(grep -c '^bootout ' "$LAUNCHCTL_TRACE")"
        [ "$bootouts" -eq 1 ]
        return $?
    fi
    printf '%s\\n' 'Bootstrap failed: 5: Input/output error' >&2
    return 5
}
service_restart
""",
        env={"LAUNCHCTL_TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert trace.read_text().splitlines() == [
        "bootout --wait gui/501/com.corsia.prod",
        f"bootstrap gui/501 {trace}.plist",
        "bootout --wait gui/501/com.corsia.prod",
    ]
    assert Path(f"{trace}.plist").read_text() == "old plist\n"
    assert "did not confirm teardown completed" in result.stderr
    assert "ingress remains held" in result.stderr


def test_service_restore_timeout_does_not_offer_unsafe_manual_bootstrap(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "launchctl.log"
    result = _run_bash(
        _SERVICE_RESTART_PREAMBLE
        + """
: > "$LAUNCHCTL_TRACE"
service_is_loaded() {
    bootouts="$(grep -c '^bootout ' "$LAUNCHCTL_TRACE" || true)"
    [ "$bootouts" -lt 2 ]
}

install_preserved_service_definition() {
    printf 'restore-plist\\n' >> "$LAUNCHCTL_TRACE"
    cp -p "$1" "$2"
}
launchctl() {
    printf '%s\\n' "$*" >> "$LAUNCHCTL_TRACE"
    if [ "$1" = bootout ]; then
        bootouts="$(grep -c '^bootout ' "$LAUNCHCTL_TRACE")"
        [ "$bootouts" -eq 1 ]
        return $?
    fi
    printf '%s\\n' 'Bootstrap failed: 64: invalid request' >&2
    return 64
}
service_restart
""",
        env={"LAUNCHCTL_TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert trace.read_text().splitlines() == [
        "bootout --wait gui/501/com.corsia.prod",
        f"bootstrap gui/501 {trace}.plist",
        "restore-plist",
        "bootout --wait gui/501/com.corsia.prod",
    ]
    assert Path(f"{trace}.plist").read_text() == "old plist\n"
    assert "did not confirm teardown completed" in result.stderr
    assert "ingress remains held" in result.stderr
    assert "Manual recovery: launchctl bootstrap" not in result.stderr
