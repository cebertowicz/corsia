"""A fatal signal leaves every thread's Python stack in the backend's log, not just a dead process.

SIGSEGV and friends inside native code end the process with no line of their own, so the
Python code that was running is never recorded. ``main`` enables ``faulthandler`` at import;
these runs crash on purpose and read the log the way the launchers write it.
"""

from __future__ import annotations

from pathlib import Path
import os
import signal
import subprocess
import sys

BACKEND = Path(__file__).resolve().parents[1]

# Run from a file so the dumped stack names a real function, as a real crash would.
# "direct" proves the enable path itself; "main" proves the backend turns it on when imported.
_SCRIPT = """
import ctypes
import faulthandler
import sys

if sys.argv[1] == "main":
    import main  # noqa: F401  the startup wiring under test
else:
    faulthandler.disable()  # so the dump below can only come from our own call
    from infra.log_format import enable_crash_stack_dumps
    enable_crash_stack_dumps()

assert faulthandler.is_enabled()


def crash():
    ctypes.string_at(0)


crash()
"""


def _crash_log(tmp_path: Path, mode: str) -> tuple[int, str]:
    script = tmp_path / "crash_and_log.py"
    script.write_text(_SCRIPT)
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    env = {**os.environ, "HOME": str(tmp_path / "home"), "PYTHONPATH": str(BACKEND),
           "CORSIA_WORKSPACE": str(workspace), "CORSIA_STATE_DIR": str(state),
           "CORSIA_TEST_MODE": "1", "CORSIA_ANALYTICS_DISABLE": "1"}
    env.pop("PYTHONFAULTHANDLER", None)
    # The launchers' `exec >>service.log 2>&1`: one open file on both descriptors before
    # the interpreter starts, which is the descriptor faulthandler writes its dump to.
    log_file = tmp_path / "service.log"
    with log_file.open("a") as handle:
        result = subprocess.run(
            [sys.executable, str(script), mode], cwd=BACKEND, env=env,
            stdout=handle, stderr=subprocess.STDOUT, timeout=120,
        )
    return result.returncode, log_file.read_text()


def test_direct_enable_dumps_every_thread_stack(tmp_path: Path) -> None:
    returncode, log = _crash_log(tmp_path, "direct")

    assert returncode == -signal.SIGSEGV
    assert "Fatal Python error" in log
    assert "most recent call first" in log
    assert "in crash" in log


def test_importing_the_backend_enables_the_dump(tmp_path: Path) -> None:
    returncode, log = _crash_log(tmp_path, "main")

    assert returncode == -signal.SIGSEGV
    assert "Fatal Python error" in log
    assert "in crash" in log
