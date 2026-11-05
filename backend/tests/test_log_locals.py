"""A traceback in the server's or installer's log keeps its lines but never the values a failing frame held."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
MARKER = "synthetic-held-value-7f3a"
# loguru colours stderr under CI (CI plus GITHUB_ACTIONS), which splits the phrases asserted below.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Run from a file: loguru annotates variables only on lines it can read the source of. The value
# comes from argv, so it never appears in a source line, only in a frame's variables.
# "server" and "installer" run the real wiring, so they first prove CORSIA state is the test's own.
_SCRIPT = """
import os
import sys
from pathlib import Path
from loguru import logger

log_file = None
if sys.argv[1] == "drop":
    from infra.log_locals import drop_traceback_locals
    drop_traceback_locals()
    drop_traceback_locals()
elif sys.argv[1] in ("server", "installer"):
    from infra import config
    assert config.STATE_ROOT.resolve() == Path(os.environ["CORSIA_STATE_DIR"]).resolve()
    if sys.argv[1] == "server":
        import main  # noqa: F401  the startup wiring under test
    else:
        from cli_local_before_start import _quiet_logs
        log_file = _quiet_logs()
        assert log_file.resolve().is_relative_to(config.STATE_ROOT.resolve())


def handle(held):
    copied = {"held": held}
    raise RuntimeError("failed with " + str(len(copied)) + " value")


try:
    handle(sys.argv[2])
except RuntimeError:
    logger.exception("request failed")
if log_file is not None:
    logger.remove()  # close the sink so the file is complete
    sys.stderr.write(log_file.read_text())
"""


def _log_exception(tmp_path: Path, mode: str) -> str:
    script = tmp_path / "raise_and_log.py"
    script.write_text(_SCRIPT)
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    env = {**os.environ, "PYTHONPATH": str(BACKEND), "CORSIA_WORKSPACE": str(workspace),
           "CORSIA_STATE_DIR": str(state), "CORSIA_TEST_MODE": "1", "CORSIA_ANALYTICS_DISABLE": "1"}
    result = subprocess.run(
        [sys.executable, str(script), mode, MARKER], cwd=BACKEND, env=env,
        capture_output=True, text=True, timeout=60, check=True,
    )
    return _ANSI.sub("", result.stderr)


@pytest.mark.parametrize(
    "mode, shows_value",
    [("default", True), ("drop", False), ("server", False), ("installer", False)],
)
def test_traceback_keeps_lines_but_drops_frame_values(tmp_path: Path, mode: str, shows_value: bool) -> None:
    stderr = _log_exception(tmp_path, mode)

    assert "request failed" in stderr
    assert "Traceback" in stderr and "in handle" in stderr
    assert "RuntimeError: failed with 1 value" in stderr
    assert (MARKER in stderr) is shows_value
