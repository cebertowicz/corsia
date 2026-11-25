"""A disposable backend starts inside a sandbox that denies writes to ~/Library/Logs.

Agents start e2e and other disposable backends from their own shell, whose
Seatbelt profile denies every write under Library/Logs. A test instance keeps
its logs under its own state root, so it must still start there.
"""
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest

BACKEND = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS-only")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _sandboxed(profile: str, command: list[str], **kwargs):
    return subprocess.Popen(["/usr/bin/sandbox-exec", "-p", profile, *command], **kwargs)


def test_test_instance_starts_when_library_logs_is_write_denied(tmp_path):
    home = tmp_path / "home"
    logs = home / "Library" / "Logs"
    home.mkdir()
    profile = f'(version 1)(allow default)(deny file-write* (subpath "{logs}"))'

    probe = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, "/bin/mkdir", "-p", str(logs / "CORSIA")],
        capture_output=True, text=True, check=False,
    )
    if probe.returncode == 71 and "sandbox_apply: Operation not permitted" in probe.stderr:
        if os.environ.get("CORSIA_REQUIRE_SAFEHOUSE") == "1":
            pytest.fail("required nested sandbox could not be applied")
        pytest.skip("nested macOS sandbox unavailable")
    assert probe.returncode != 0, "the profile must deny the write it stands in for"

    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    port = _free_port()
    env = {
        **os.environ,
        "HOME": str(home),
        "CORSIA_TEST_MODE": "1",
        "CORSIA_ALLOW_HOME_WORKSPACE_IN_CI": "1",
        "CORSIA_ANALYTICS_DISABLE": "1",
        "CORSIA_WORKSPACE": str(workspace),
        "CORSIA_STATE_DIR": str(state),
    }
    boot_log = tmp_path / "boot.log"
    with boot_log.open("w") as log:
        proc = _sandboxed(
            profile,
            [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=BACKEND, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            ready = False
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and proc.poll() is None:
                with socket.socket() as sock:
                    sock.settimeout(0.2)
                    if sock.connect_ex(("127.0.0.1", port)) == 0:
                        ready = True
                        break
                time.sleep(0.2)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    assert ready, f"backend exited {proc.returncode} before listening:\n{boot_log.read_text()[-4000:]}"
    assert (state / "logs").is_dir()
    assert not logs.exists()
