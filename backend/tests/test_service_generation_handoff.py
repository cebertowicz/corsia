from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
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
launchctl_bootout_wait() { launchctl bootout "$1"; }
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


def _orphan_sleeper() -> int:
    """A live process launchd (PID 1) reaps once it is killed, like a service."""
    return int(subprocess.run(
        ["/bin/sh", "-c", f'"{sys.executable}" -c "import time; time.sleep(60)" '
         "</dev/null >/dev/null 2>&1 & echo $!"],
        check=True, capture_output=True, text=True,
    ).stdout)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _kill(pid: int) -> None:
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass


_UNLOADED = None


def _run_bootout_wait(
    tmp_path: Path, pid: int | str | None, bootout: str,
    after: list[int | str | None] | None = None,
) -> subprocess.CompletedProcess:
    """Run the real wrapper against a fake launchctl.

    ``pid`` is what `print` reports before bootout ("" for a loaded job with no
    PID line); ``after`` lists what each later `print` reports, the last one
    repeating. By default launchd stops listing the job once it is booted out.
    """
    trace = tmp_path / "launchctl.log"
    replies = [pid, *(after if after is not None else [_UNLOADED])]
    fake_launchctl = tmp_path / "launchctl"
    fake_launchctl.write_text(
        f"#!{sys.executable}\n"
        "import os, subprocess, sys\n"
        f"trace = {str(trace)!r}\n"
        f"replies = {replies!r}\n"
        "open(trace, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == 'print':\n"
        "    count = sum(line.startswith('print') for line in open(trace)) - 1\n"
        "    reply = replies[min(count, len(replies) - 1)]\n"
        "    if reply is None:\n"
        "        sys.exit(113)\n"
        "    line = f'\\tpid = {reply}\\n' if reply != '' else ''\n"
        "    print('gui/501/com.corsia.prod = {\\n' + line + '}')\n"
        "    sys.exit(0)\n"
        "if sys.argv[1:] != ['bootout', 'gui/501/com.corsia.prod']:\n"
        "    sys.exit(64)\n"
        f"{bootout}\n"
    )
    fake_launchctl.chmod(0o755)
    return _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
source scripts/corsia.d/service.sh
SERVICE_PID_EXIT_WAIT_SECONDS=2
launchctl_bootout_wait gui/501/com.corsia.prod
""",
        env={"PATH": f"{tmp_path}:/usr/bin:/bin", "PYTHON": sys.executable},
    )


def test_launchctl_bootout_wait_returns_once_the_job_process_exits(tmp_path: Path) -> None:
    pid = _orphan_sleeper()
    try:
        result = _run_bootout_wait(tmp_path, pid, f"os.kill({pid}, 9)")
    finally:
        _kill(pid)

    assert result.returncode == 0, result.stderr
    # No `--wait`: macOS 15 rejects it as an unrecognized target specifier.
    assert (tmp_path / "launchctl.log").read_text().splitlines() == [
        "print gui/501/com.corsia.prod",
        "bootout gui/501/com.corsia.prod",
        "print gui/501/com.corsia.prod",
    ]


def test_launchctl_bootout_wait_follows_a_keepalive_restart(tmp_path: Path) -> None:
    # KeepAlive restarted the job between the first print and the bootout: the
    # PID read first is gone, and the process bootout stopped is a new one.
    gone = _orphan_sleeper()
    os.kill(gone, 9)
    restarted = _orphan_sleeper()
    stop_later = (
        f"subprocess.Popen(['/bin/sh', '-c', 'sleep 0.6; kill -9 {restarted}'])"
    )
    try:
        result = _run_bootout_wait(
            tmp_path, gone, stop_later, after=[restarted, _UNLOADED],
        )
        still_running = _alive(restarted)
    finally:
        _kill(restarted)

    assert result.returncode == 0, result.stderr
    assert not still_running


def test_launchctl_bootout_wait_waits_for_launchd_to_unload_a_job_with_no_pid(
    tmp_path: Path,
) -> None:
    result = _run_bootout_wait(tmp_path, "", "sys.exit(0)", after=["", "", _UNLOADED])

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "launchctl.log").read_text().splitlines()[-3:] == [
        "print gui/501/com.corsia.prod",
        "print gui/501/com.corsia.prod",
        "print gui/501/com.corsia.prod",
    ]


def test_launchctl_bootout_wait_times_out_while_launchd_still_lists_the_job(
    tmp_path: Path,
) -> None:
    result = _run_bootout_wait(tmp_path, "", "sys.exit(0)", after=[""])

    assert result.returncode == 124


def test_launchctl_bootout_wait_refuses_an_unreadable_pid_before_bootout(
    tmp_path: Path,
) -> None:
    result = _run_bootout_wait(tmp_path, "not-a-pid", "sys.exit(0)")

    assert result.returncode == 1
    assert "cannot read the PID" in result.stderr
    assert (tmp_path / "launchctl.log").read_text().splitlines() == [
        "print gui/501/com.corsia.prod",
    ]


def test_launchctl_bootout_wait_times_out_while_the_job_process_lives(tmp_path: Path) -> None:
    pid = _orphan_sleeper()
    try:
        result = _run_bootout_wait(tmp_path, pid, "sys.exit(0)")
    finally:
        _kill(pid)

    assert result.returncode == 124


def test_launchctl_bootout_wait_reads_a_pid_line_with_trailing_space(tmp_path: Path) -> None:
    pid = _orphan_sleeper()
    try:
        result = _run_bootout_wait(tmp_path, f"{pid} ", "sys.exit(0)")
    finally:
        _kill(pid)

    assert result.returncode == 124


def test_launchctl_bootout_wait_counts_a_process_it_cannot_signal_as_alive(
    tmp_path: Path,
) -> None:
    # PID 1 is root's launchd: kill(1, 0) fails with EPERM, never ESRCH.
    if os.getuid() == 0:
        pytest.skip("root may signal PID 1")
    result = _run_bootout_wait(tmp_path, 1, "sys.exit(0)")

    assert result.returncode == 124


def test_launchctl_bootout_wait_returns_launchctl_failure(tmp_path: Path) -> None:
    pid = _orphan_sleeper()
    try:
        result = _run_bootout_wait(tmp_path, pid, "sys.stderr.write('Boot-out failed: 5\\n'); sys.exit(5)")
    finally:
        _kill(pid)

    assert result.returncode == 5
    assert "Boot-out failed: 5" in result.stderr


def test_bootout_service_and_wait_takes_no_pid_snapshot_of_its_own(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "launchctl.log"
    result = _run_bash(
        """
warn() { :; }
ok() { :; }
fail() { :; }
source scripts/corsia.d/service.sh
launchctl_bootout_wait() { launchctl bootout "$1"; }
service_loaded_pid() { printf 'PID snapshot must not be used' >&2; return 1; }
launchctl() { printf '%s\\n' "$*" >> "$TRACE"; }
bootout_service_and_wait gui/501 com.corsia.prod
""",
        env={"TRACE": str(trace)},
    )

    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == [
        "bootout gui/501/com.corsia.prod",
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
    printf 'bootout %s\\n' "$1" >> "$LAUNCHCTL_TRACE"
    return 124
}
launchctl() { printf '%s\\n' "$*" >> "$LAUNCHCTL_TRACE"; }
service_restart
""",
        env={"LAUNCHCTL_TRACE": str(trace)},
    )

    assert result.returncode != 0
    assert trace.read_text().splitlines() == [
        "bootout gui/501/com.corsia.prod",
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
        "bootout gui/501/com.corsia.prod",
        f"bootstrap gui/501 {trace}.plist",
        "bootout gui/501/com.corsia.prod",
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
        "bootout gui/501/com.corsia.prod",
        f"bootstrap gui/501 {trace}.plist",
        "restore-plist",
        "bootout gui/501/com.corsia.prod",
    ]
    assert Path(f"{trace}.plist").read_text() == "old plist\n"
    assert "did not confirm teardown completed" in result.stderr
    assert "ingress remains held" in result.stderr
    assert "Manual recovery: launchctl bootstrap" not in result.stderr
