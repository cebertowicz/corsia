"""The bundled backend launcher tells the backend its port and workspace."""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
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
    # Only the service start is faked. Anything else (the launcher's JSON parse
    # of service.json) runs on a real interpreter.
    interpreter.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *uvicorn*)\n"
        '    printf "BACKEND_PORT=%s\\n" "${BACKEND_PORT-unset}"\n'
        '    printf "CORSIA_WORKSPACE=%s\\n" "${CORSIA_WORKSPACE-unset}"\n'
        '    printf "ARGS=%s\\n" "$*" ;;\n'
        f'  *) exec "{sys.executable}" "$@" ;;\n'
        "esac\n"
    )
    interpreter.chmod(interpreter.stat().st_mode | stat.S_IXUSR)
    return launcher


def _launch(
    launcher: Path, state: Path, *arguments: str, env: dict[str, str] | None = None
) -> str:
    subprocess.run(
        [str(launcher), *arguments],
        env={"PATH": "/usr/bin:/bin", "CORSIA_STATE_DIR": str(state), **(env or {})},
        check=True,
        timeout=30,
    )
    return (state / "logs" / "service.log").read_text()


def _service_file(state: Path, content: str | dict) -> None:
    state.mkdir(parents=True, exist_ok=True)
    text = content if isinstance(content, str) else json.dumps(content)
    (state / "service.json").write_text(text)


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


def test_launcher_exports_the_workspace_from_service_json(tmp_path):
    launcher = _bundle_with_recording_python(tmp_path)
    state = tmp_path / "state"
    _service_file(state, {"workspace": "/Users/someone/corsia", "port": 47895})

    log = _launch(launcher, state, "--host", "127.0.0.1")

    assert "CORSIA_WORKSPACE=/Users/someone/corsia" in log
    assert log.splitlines()[0].endswith("port=47895 workspace=/Users/someone/corsia")
    assert "BACKEND_PORT=47895" in log
    assert "--port 47895" in log


def test_launcher_prefers_the_explicit_port_and_workspace_over_service_json(tmp_path):
    launcher = _bundle_with_recording_python(tmp_path)
    state = tmp_path / "state"
    _service_file(state, {"workspace": "/from/file", "port": 47895})

    log = _launch(
        launcher,
        state,
        "--port",
        "18788",
        env={"CORSIA_WORKSPACE": "/from/environment"},
    )

    assert "CORSIA_WORKSPACE=/from/environment" in log
    assert "BACKEND_PORT=18788" in log
    assert "47895" not in log


def test_launcher_without_service_json_leaves_the_workspace_to_the_backend(tmp_path):
    launcher = _bundle_with_recording_python(tmp_path)

    log = _launch(launcher, tmp_path / "state", "--port", "18788")

    assert "CORSIA_WORKSPACE=unset" in log
    assert log.splitlines()[0].endswith("workspace=unset")


def test_launcher_parses_service_json_and_never_executes_it(tmp_path):
    launcher = _bundle_with_recording_python(tmp_path)
    marker = tmp_path / "ran"
    # Valid JSON whose value and key name are shell syntax, then a file that
    # is a shell script outright: neither may run.
    first_state = tmp_path / "first"
    value = f"/w/$(touch {marker})`touch {marker}`"
    _service_file(
        first_state,
        {"workspace": value, f"$(touch {marker})": "x", "port": 18790},
    )
    second_state = tmp_path / "second"
    _service_file(second_state, f"workspace=/w; touch {marker}\n")

    first = _launch(launcher, first_state, "--host", "127.0.0.1")
    second = _launch(launcher, second_state, "--host", "127.0.0.1")

    assert not marker.exists()
    assert f"CORSIA_WORKSPACE={value}" in first
    assert "service.json unreadable" in second
    assert "CORSIA_WORKSPACE=unset" in second


def test_launcher_ignores_and_logs_unknown_keys_without_their_values(tmp_path):
    launcher = _bundle_with_recording_python(tmp_path)
    state = tmp_path / "state"
    _service_file(
        state,
        {
            "workspace": "/w",
            "CORSIA_STATE_DIR": "/elsewhere",
            "ANTHROPIC_API_KEY": "sk-secret-value",
            "PATH": "/evil",
        },
    )

    log = _launch(launcher, state, "--port", "18788")

    for key in ("CORSIA_STATE_DIR", "ANTHROPIC_API_KEY", "PATH"):
        assert f"ignoring unsupported key {key}" in log
    assert "sk-secret-value" not in log
    assert "/evil" not in log and "/elsewhere" not in log
    assert f"state_dir={state} " in log.splitlines()[0]
    assert "CORSIA_WORKSPACE=/w" in log


def test_launcher_ignores_invalid_values(tmp_path):
    launcher = _bundle_with_recording_python(tmp_path)
    state = tmp_path / "state"
    _service_file(state, {"workspace": "relative/path", "port": "80"})

    log = _launch(launcher, state)

    assert "CORSIA_WORKSPACE=unset" in log
    assert "BACKEND_PORT=unset" in log
    assert "invalid workspace value" in log and "invalid port value" in log
