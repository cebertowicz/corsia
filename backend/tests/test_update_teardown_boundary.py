"""The update lifecycle communicates uncertain teardown to its watcher."""

import os
from pathlib import Path

import pytest

from api.restart_shell import spawn_restart_lifecycle


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [("timeout", 200), ("unloaded", 200), ("loaded", 1), ("success", 200),
     ("refusal", 1), ("refusal200", 1)],
)
def test_update_exit_distinguishes_teardown(tmp_path, scenario, expected):
    service = Path(__file__).resolve().parents[2] / "scripts/corsia.d/service.sh"
    script = tmp_path / "corsia"
    script.write_text(f'''#!/bin/bash
source "{service}"
fail() {{ :; }}
launchctl_bootout_wait() {{
    case "$SCENARIO" in timeout) return 124 ;; success) return 0 ;; *) return 1 ;; esac
}}
service_is_loaded() {{ [ "$SCENARIO" = loaded ]; }}
if [ "$SCENARIO" = refusal200 ]; then exit 200; fi
if [ "$SCENARIO" != refusal ]; then
    bootout_service_and_wait domain label || :
fi
exit 1
''')
    script.chmod(0o755)
    curl = tmp_path / "curl"
    curl.write_text("#!/bin/bash\nexit 0\n")
    curl.chmod(0o755)
    environment = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
                   "CORSIA_RESTART_BACKEND_PORT": "4111", "SCENARIO": scenario}
    with (tmp_path / "lifecycle.log").open("w") as log:
        child = spawn_restart_lifecycle(script, ["restart-prepared"], environment,
                                        "a" * 32, log, caller_credential="test-credential")
        assert child.wait(timeout=5) == expected
