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
