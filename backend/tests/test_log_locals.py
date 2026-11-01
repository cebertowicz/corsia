"""A traceback in the server's log keeps its lines but never the values a failing frame held."""

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
_SCRIPT = """
import sys
from loguru import logger

if sys.argv[1] == "drop":
    from infra.log_locals import drop_traceback_locals
    drop_traceback_locals()
    drop_traceback_locals()


def handle(held):
    copied = {"held": held}
    raise RuntimeError("failed with " + str(len(copied)) + " value")


try:
    handle(sys.argv[2])
except RuntimeError:
    logger.exception("request failed")
"""


def _log_exception(tmp_path: Path, mode: str) -> str:
    script = tmp_path / "raise_and_log.py"
    script.write_text(_SCRIPT)
    env = {**os.environ, "PYTHONPATH": str(BACKEND)}
    result = subprocess.run(
        [sys.executable, str(script), mode, MARKER], cwd=BACKEND, env=env,
        capture_output=True, text=True, timeout=60, check=True,
    )
    return _ANSI.sub("", result.stderr)


@pytest.mark.parametrize("mode, shows_value", [("default", True), ("drop", False)])
def test_traceback_keeps_lines_but_drops_frame_values(tmp_path: Path, mode: str, shows_value: bool) -> None:
    stderr = _log_exception(tmp_path, mode)

    assert "request failed" in stderr
    assert "Traceback" in stderr and "in handle" in stderr
    assert "RuntimeError: failed with 1 value" in stderr
    assert (MARKER in stderr) is shows_value
