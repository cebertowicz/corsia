"""The bundled backend launcher tells the backend which port it serves on."""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path


LAUNCHER = Path(__file__).resolve().parents[2] / "desktop" / "scripts" / "corsia-service"


def _bundle_with_recording_python(tmp_path: Path) -> Path:
    resources = tmp_path / "Resources"
    (resources / "bin").mkdir(parents=True)
    (resources / "python" / "bin").mkdir(parents=True)
    launcher = resources / "bin" / "corsia-service"
    shutil.copy(LAUNCHER, launcher)
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    interpreter = resources / "python" / "bin" / "python3"
    interpreter.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *uvicorn*) printf "BACKEND_PORT=%s\\n" "${BACKEND_PORT-unset}" ;;\n'
        "esac\n"
    )
    interpreter.chmod(interpreter.stat().st_mode | stat.S_IXUSR)
    return launcher


def _launch(launcher: Path, state: Path, *arguments: str) -> str:
    subprocess.run(
        [str(launcher), *arguments],
        env={"PATH": "/usr/bin:/bin", "CORSIA_STATE_DIR": str(state)},
        check=True,
        timeout=30,
    )
    return (state / "logs" / "service.log").read_text()


def test_launcher_exports_the_port_it_serves_on(tmp_path):
    launcher = _bundle_with_recording_python(tmp_path)

    log = _launch(launcher, tmp_path / "state", "--host", "127.0.0.1", "--port", "18788")

    assert "BACKEND_PORT=18788" in log


def test_launcher_exports_the_port_given_as_one_argument(tmp_path):
    launcher = _bundle_with_recording_python(tmp_path)

    log = _launch(launcher, tmp_path / "state", "--port=18789")

    assert "BACKEND_PORT=18789" in log


def test_launcher_leaves_backend_port_unset_without_a_port(tmp_path):
    launcher = _bundle_with_recording_python(tmp_path)

    log = _launch(launcher, tmp_path / "state", "--host", "127.0.0.1")

    assert "BACKEND_PORT=unset" in log
