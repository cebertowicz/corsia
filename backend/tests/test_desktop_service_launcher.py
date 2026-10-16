"""The bundled backend launcher tells the backend its port and workspace."""

from __future__ import annotations

import json
import re
import shutil
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest


LAUNCHER = Path(__file__).resolve().parents[2] / "desktop" / "scripts" / "corsia-service"


def _bundle_with_recording_python(tmp_path: Path) -> Path:
    resources = tmp_path / "Resources"
    (resources / "bin").mkdir(parents=True)
    (resources / "python" / "bin").mkdir(parents=True)
    (tmp_path / "Info.plist").write_text(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
        "<plist version=\"1.0\"><dict>"
        "<key>CFBundleShortVersionString</key><string>1.2.3</string>"
        "<key>CFBundleVersion</key><string>456</string>"
        "</dict></plist>\n"
    )
    launchctl = resources / "bin" / "launchctl"
    launchctl.write_text("#!/bin/sh\nexit 0\n")
    launchctl.chmod(0o755)
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
        '    printf "CORSIA_SERVICE_GENERATION=%s\\n" "${CORSIA_SERVICE_GENERATION-unset}"\n'
        '    printf "PATH=%s\\n" "$PATH"\n'
        '    printf "GIT_CONFIG_NOSYSTEM=%s\\n" "${GIT_CONFIG_NOSYSTEM-unset}"\n'
        '    printf "GIT_TERMINAL_PROMPT=%s\\n" "${GIT_TERMINAL_PROMPT-unset}"\n'
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
        env={
            "PATH": "/usr/bin:/bin",
            "CORSIA_STATE_DIR": str(state),
            "CORSIA_TEST_LAUNCHCTL": str(launcher.parent / "launchctl"),
            **(env or {}),
        },
        check=True,
        timeout=30,
    )
    return (state / "logs" / "service.log").read_text()


def _service_file(state: Path, content: str | dict) -> None:
    state.mkdir(parents=True, exist_ok=True)
    text = content if isinstance(content, str) else json.dumps(content)
    (state / "service.json").write_text(text)


def test_launcher_starts_without_arguments_on_loopback_and_uses_bundle_generation(tmp_path):
    launcher = _bundle_with_recording_python(tmp_path)
    state = tmp_path / "state"
    _service_file(state, {"workspace": "/Users/someone/corsia", "port": 18788})

    log = _launch(launcher, state)

    assert "BACKEND_PORT=18788" in log
    assert "CORSIA_SERVICE_GENERATION=1.2.3+456" in log
    assert "--host 127.0.0.1 --port 18788" in log


def test_launcher_exports_the_port_it_serves_on(tmp_path):
    launcher = _bundle_with_recording_python(tmp_path)

    log = _launch(launcher, tmp_path / "state", "--host", "127.0.0.1", "--port", "18788")

    assert "BACKEND_PORT=18788" in log


def test_launcher_prefers_its_bundled_git_and_disables_system_config(tmp_path):
    launcher = _bundle_with_recording_python(tmp_path)
    resources = launcher.parent.parent
    git_core = resources / "libexec" / "git-core"
    git_core.mkdir(parents=True)
    (git_core / "git").write_text("#!/bin/sh\n")
    (git_core / "git").chmod(0o755)
    # The person's login PATH reaches the system Git first.
    (resources / "backend").mkdir()
    (resources / "backend" / "cli_location.py").write_text(
        "def login_shell_path():\n    return '/usr/bin:/opt/homebrew/bin'\n"
    )

    log = _launch(launcher, tmp_path / "state", "--port", "18788")

    assert (
        f"PATH={git_core}:/usr/bin:/opt/homebrew/bin:{resources / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin\n"
        in log
    )
    assert f"CORSIA bundled backend: git={git_core / 'git'}\n" in log
    assert "GIT_CONFIG_NOSYSTEM=1" in log
    assert "GIT_TERMINAL_PROMPT=0" in log


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
    assert log.splitlines()[0].endswith(
        "port=47895 workspace=/Users/someone/corsia generation=1.2.3+456"
    )
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
    assert log.splitlines()[0].endswith("workspace=unset generation=1.2.3+456")


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


def test_launcher_allows_a_service_from_its_own_bundle(tmp_path):
    launcher = _bundle_with_recording_python(tmp_path)
    own_launchctl = tmp_path / "own-launchctl"
    own_launchctl.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  list) printf 'PID\\tStatus\\tLabel\\n123\\t0\\tai.corsia.service\\n' ;;\n"
        f"  print) printf 'ai.corsia.service = {{\\n\\tprogram = {launcher}\\n}}\\n' ;;\n"
        "esac\n"
    )
    own_launchctl.chmod(0o755)

    log = _launch(
        launcher,
        tmp_path / "state",
        "--port",
        "18788",
        env={"CORSIA_TEST_LAUNCHCTL": str(own_launchctl)},
    )

    assert "BACKEND_PORT=18788" in log


def test_launcher_refuses_a_foreign_corsia_job_without_respawning(tmp_path):
    launcher = _bundle_with_recording_python(tmp_path)
    foreign_launchctl = tmp_path / "foreign-launchctl"
    foreign_launchctl.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  list) printf 'PID\\tStatus\\tLabel\\n-\\t0\\tcom.corsia.prod\\n' ;;\n"
        "  print) printf 'com.corsia.prod = {\\n\\tpath = /opt/corsia/bin/corsia\\n}\\n' ;;\n"
        "esac\n"
    )
    foreign_launchctl.chmod(0o755)

    log = _launch(
        launcher,
        tmp_path / "state",
        "--port",
        "18788",
        env={"CORSIA_TEST_LAUNCHCTL": str(foreign_launchctl)},
    )

    assert "foreign CORSIA service com.corsia.prod owns this Mac" in log
    assert "BACKEND_PORT=" not in log


def test_launcher_refuses_when_corsia_service_inspection_fails(tmp_path):
    for failure in ("list", "print"):
        launcher = _bundle_with_recording_python(tmp_path / failure)
        failing_launchctl = tmp_path / f"{failure}-launchctl"
        failing_launchctl.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            f"  list) [ '{failure}' = list ] && exit 1; printf 'PID\\tStatus\\tLabel\\n-\\t0\\tcom.corsia.prod\\n' ;;\n"
            "  print) exit 1 ;;\n"
            "esac\n"
        )
        failing_launchctl.chmod(0o755)

        log = _launch(
            launcher,
            tmp_path / f"{failure}-state",
            "--port",
            "18788",
            env={"CORSIA_TEST_LAUNCHCTL": str(failing_launchctl)},
        )

        assert "could not inspect CORSIA service" in log
        assert "BACKEND_PORT=" not in log


OWNERSHIP_FIXTURES = json.loads(
    (LAUNCHER.parents[1] / "tests/fixtures/launchctl-ownership.json").read_text()
)


@pytest.mark.parametrize(
    "fixture,self_identity",
    [(case, False) for case in OWNERSHIP_FIXTURES]
    + [(next(case for case in OWNERSHIP_FIXTURES if case["name"] == "private_environment"), True)],
    ids=[case["name"] for case in OWNERSHIP_FIXTURES] + ["private_environment_self"],
)
def test_launcher_shared_ownership_fixtures(tmp_path, fixture, self_identity):
    bundle = tmp_path / "CORSIA With Spaces.app"
    launcher = _bundle_with_recording_python(bundle / "Contents")
    details = fixture["details"].replace("@BUNDLE@", str(bundle))
    fake = tmp_path / "fixture-launchctl"
    fake.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  list) printf '123 0 ai.corsia.service\\n' ;;\n"
        f"  print) printf '%s' {shlex.quote(details)} ;;\n"
        "esac\n"
    )
    fake.chmod(0o755)
    env = {"CORSIA_TEST_LAUNCHCTL": str(fake)}
    if self_identity or fixture.get("xpc_service_name"):
        env["XPC_SERVICE_NAME"] = fixture.get("xpc_service_name", "ai.corsia.service")
    log = _launch(launcher, tmp_path / "state", env=env)
    assert ("ARGS=" in log) is fixture["owned"]
    assert "launchctl print ai.corsia.service" in log
    for sensitive in fixture.get("sensitive_values", []):
        assert sensitive not in log
    for ignored in fixture.get("ignored_metadata", []):
        assert ignored.replace("@BUNDLE@", str(bundle)) not in log
    assert "SECRET" not in log
    assert "EnvironmentVariables" not in log
    if fixture["name"] == "private_environment":
        assert "program = /foreign/service" in log
        assert "state = running" in log
        assert "pid = 123" in log
    expected_identity = "ai.corsia.service" if self_identity or fixture.get("xpc_service_name") else "unset"
    assert f"XPC_SERVICE_NAME={expected_identity}" in log
    if not fixture["owned"]:
        assert "refused to start:" in log


@pytest.mark.parametrize("details", ["state = first registration", "program = /foreign/service"])
def test_launcher_parses_inherited_xpc_when_parent_is_not_launchd(tmp_path, details):
    launcher = _bundle_with_recording_python(tmp_path / "CORSIA.app/Contents")
    job = f"ai.corsia.service = {{\n {details}\n}}\n"
    fake = tmp_path / "self-launchctl"
    fake.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        " list) printf '123 0 ai.corsia.service\\n' ;;\n"
        f" print) printf '%s\\n' {shlex.quote(job)} ;;\n"
        "esac\n"
    )
    fake.chmod(0o755)
    log = _launch(launcher, tmp_path / "state", env={
        "CORSIA_TEST_LAUNCHCTL": str(fake), "XPC_SERVICE_NAME": "ai.corsia.service",
    })
    assert "ARGS=" not in log
    assert "XPC_SERVICE_NAME=ai.corsia.service" in log
    assert details in log
    assert "refused to start:" in log


def test_launcher_self_identity_does_not_exempt_other_corsia_jobs(tmp_path):
    launcher = _bundle_with_recording_python(tmp_path / "CORSIA.app/Contents")
    fake = tmp_path / "mixed-launchctl"
    fake.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        " list) printf '123 0 ai.corsia.service\\n456 0 com.corsia.prod\\n' ;;\n"
        ' print) case "$2" in\n'
        f"  */ai.corsia.service) printf 'ai.corsia.service = {{\\n program = %s\\n}}\\n' {shlex.quote(str(launcher))} ;;\n"
        "  *) printf 'com.corsia.prod = {\\n program = /foreign/service\\n}\\n' ;;\n"
        " esac ;;\n"
        "esac\n"
    )
    fake.chmod(0o755)
    log = _launch(launcher, tmp_path / "state", env={
        "CORSIA_TEST_LAUNCHCTL": str(fake), "XPC_SERVICE_NAME": "ai.corsia.service",
    })
    assert "foreign CORSIA service com.corsia.prod" in log
    assert "ARGS=" not in log


def test_launcher_self_predicate_requires_launchd_parent():
    helper = re.search(r"is_launchd_self\(\) \{\n.*?\n\}", LAUNCHER.read_text(), re.DOTALL).group()
    cases = [("ai.corsia.service", 1, True), ("com.corsia.prod", 1, False)]
    cases += [
        ("ai.corsia.service", case["parent_pid"], case["is_self"])
        for case in OWNERSHIP_FIXTURES if "is_self" in case
    ]
    for label, parent_pid, expected in cases:
        result = subprocess.run(
            ["/bin/sh", "-c", helper + '\nis_launchd_self "$1" "$2"', "test", label, str(parent_pid)],
            env={"XPC_SERVICE_NAME": "ai.corsia.service"},
            check=False,
            timeout=5,
        )
        assert (result.returncode == 0) is expected


def test_launcher_ignores_invalid_values(tmp_path):
    launcher = _bundle_with_recording_python(tmp_path)
    state = tmp_path / "state"
    _service_file(state, {"workspace": "relative/path", "port": "80"})

    log = _launch(launcher, state)

    assert "CORSIA_WORKSPACE=unset" in log
    assert "BACKEND_PORT=unset" in log
    assert "invalid workspace value" in log and "invalid port value" in log
