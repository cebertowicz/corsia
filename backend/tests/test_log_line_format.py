"""The real startup logging configuration keeps all three streams traceable."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from uvicorn.config import LOGGING_CONFIG


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
import uvicorn
uvicorn.Config("main:app")
import main
from infra.log_format import configure_stdlib_logging
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


@pytest.mark.parametrize("custom_config", [False, True])
def test_uvicorn_cli_respects_logging_options(tmp_path: Path, custom_config: bool) -> None:
    workspace, state = tmp_path / "workspace", tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    env = {**os.environ, "CORSIA_WORKSPACE": str(workspace), "CORSIA_STATE_DIR": str(state),
           "CORSIA_TEST_MODE": "1", "CORSIA_ANALYTICS_DISABLE": "1"}
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    log_path = tmp_path / "backend.log"
    args = ["--log-level", "warning", "--no-access-log"]
    if custom_config:
        config = json.loads(json.dumps(LOGGING_CONFIG))
        config["formatters"]["default"]["fmt"] = "CUSTOM %(levelprefix)s %(message)s"
        config["formatters"]["access"]["fmt"] = 'CUSTOM %(client_addr)s %(request_line)s %(status_code)s'
        config_path = tmp_path / "logging.json"
        config_path.write_text(json.dumps(config))
        args = ["--log-config", str(config_path)]
    with log_path.open("w+") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port), *args],
            cwd=BACKEND, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 45
            while True:
                assert process.poll() is None, "Uvicorn exited before becoming ready"
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
                        assert response.status == 200
                    break
                except (urllib.error.URLError, TimeoutError):
                    if time.monotonic() >= deadline:
                        raise AssertionError("Uvicorn did not become ready")
                    time.sleep(0.2)
        finally:
            process.terminate()
            process.wait(timeout=15)
        log.seek(0)
        output = log.read()
    if custom_config:
        access = next(line for line in output.splitlines() if 'GET /api/health' in line)
        assert re.match(r"CUSTOM 127\.0\.0\.1:\d+ GET /api/health HTTP/1\.1 200 OK$", access)
        assert "CUSTOM INFO:     Application startup complete" in output
    else:
        assert "Application startup complete" not in output
        assert 'GET /api/health' not in output
