"""The real startup logging configuration keeps all three streams traceable."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]


def test_startup_log_lines_and_reference(tmp_path: Path) -> None:
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    env = {**os.environ, "PYTHONPATH": str(BACKEND), "CORSIA_WORKSPACE": str(workspace),
           "CORSIA_STATE_DIR": str(state), "CORSIA_TEST_MODE": "1", "CORSIA_ANALYTICS_DISABLE": "1"}
    script = '''
import logging
import os
import main
from infra.log_locals import configure_stdlib_logging
from infra.otel_logs.capture import CaptureQueue, LogCapture
from loguru import logger

configure_stdlib_logging()
capture = LogCapture(CaptureQueue())
capture.attach()
print("TEST_PID=" + str(os.getpid()), flush=True)
logger.warning("format-loguru")
logging.getLogger("format-test").warning("format-stdlib")
logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:123", "GET", "/?hidden=value", "1.1", 200)
try:
    raise ValueError("synthetic")
except ValueError:
    logging.getLogger("format-test").exception("format-exception reference=%s", "ref-test-123")
capture.detach()
'''
    result = subprocess.run([sys.executable, "-c", script], cwd=BACKEND, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=60, check=True)
    output = result.stdout
    pid_match = re.search(r"TEST_PID=(\d+)", output)
    assert pid_match is not None
    pid = pid_match.group(1)
    prefix = rf"\d{{4}}-\d{{2}}-\d{{2}} \d{{2}}:\d{{2}}:\d{{2}}\.\d{{3}} \| [A-Z]+ \| pid {pid} \| "
    for marker in ("format-loguru", "format-stdlib", "format-exception reference=ref-test-123"):
        lines = [line for line in output.splitlines() if marker in line]
        assert len(lines) == 1
        assert re.match(prefix + r"[^ ]+:[^ ]+:\d+ - ", lines[0])
    access = [line for line in output.splitlines() if 'GET / HTTP/1.1' in line]
    assert len(access) == 1
    assert re.match(prefix + r"uvicorn\.access - ", access[0])
    assert "hidden=value" not in output
