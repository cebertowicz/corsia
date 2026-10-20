"""scripts/corsia run from inside a packaged CORSIA.app (corsia.d/app_mode.sh).

Every test builds a fake app in a temp dir and puts fake ``launchctl``,
``curl``, ``lsof`` and ``ps`` first on PATH, so nothing reaches the real
launchd, a real backend, a real port or the host's process table. The fake launchd runs a job by reading the
Info.plist of whatever bundle sits at its plist's path when it starts it, so a
swap is observable as a new generation.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from api.restart_shell import LIFECYCLE_SHELL_COMMAND


REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
LABEL = "ai.corsia.service"
OPERATION_ID = "0123456789abcdef0123456789abcdef"
PORT = 47211

_FAKE_LAUNCHCTL = """
import os, plistlib, sys
from pathlib import Path

state = Path(os.environ["FAKE_LAUNCHD"])
args = sys.argv[1:]
with open(state / "calls", "a") as log:
    log.write(" ".join(args) + "\\n")
loaded = state / "loaded"


def start(plist):
    bundle = Path(plist).parents[3]
    info = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
    (state / "generation").write_text(
        info["CFBundleShortVersionString"] + "+" + info["CFBundleVersion"]
    )
    starts = int((state / "starts").read_text()) + 1
    (state / "starts").write_text(str(starts))
    pids = (state / "pids").read_text().split()
    (state / "pid").write_text(pids[starts % len(pids)])


command = args[0]
if command == "print":
    if not loaded.exists():
        sys.exit(113)
    print(args[1] + " = {")
    print("\\tpath = " + loaded.read_text())
    print("\\tenvironment = {")
    print("\\t\\tprogram = /elsewhere/bin/x")
    print("\\t}")
    print("\\tpid = " + (state / "pid").read_text())
    print("}")
elif command == "bootstrap":
    if loaded.exists() or (state / "fail_bootstrap").exists():
        sys.exit(5)
    loaded.write_text(args[2])
    start(args[2])
elif command == "bootout":
    if not loaded.exists():
        sys.exit(3)
    loaded.unlink()
elif command == "kickstart":
    if not loaded.exists():
        sys.exit(113)
    start(loaded.read_text())
else:
    sys.exit(64)
"""

_FAKE_CURL = """
import json, os, sys
from pathlib import Path

state = Path(os.environ["FAKE_LAUNCHD"])
url = next(arg for arg in reversed(sys.argv[1:]) if arg.startswith("http"))
with open(state / "curl", "a") as log:
    log.write(url.split("?")[0] + "\\n")
serving = (state / "loaded").exists()
if url.endswith("/api/health") and serving:
    failed_generation = state / "health_fail_generation"
    if failed_generation.exists() and (state / "generation").read_text() == failed_generation.read_text():
        sys.exit(22)
    sys.exit(0)
if url.endswith("/api/version") and serving:
    print(json.dumps({
        "backend": {
            "start_iso": (state / "starts").read_text(),
            "generation": (state / "generation").read_text(),
            "full_hash": "abc",
            "current_head": None,
        },
        "frontend": {"full_hash": "abc"},
    }))
    sys.exit(0)
sys.exit(22)
"""

_FAKE_LSOF = """
import os, sys
from pathlib import Path

state = Path(os.environ["FAKE_LAUNCHD"])
if any(arg.startswith("-iTCP") for arg in sys.argv) and (state / "loaded").exists():
    print((state / "pid").read_text())
    sys.exit(0)
sys.exit(1)
"""

# Only `ps -o command= -p PID`: launchd's PIDs run the backend's interpreter.
_FAKE_PS = """
import os, sys
from pathlib import Path

state = Path(os.environ["FAKE_LAUNCHD"])
if sys.argv[-1] in (state / "pids").read_text().split():
    print("python3 -m uvicorn main:app")
    sys.exit(0)
sys.exit(1)
"""

# The app binary's update modes. A control file `<mode>_exit` makes a mode fail
# with that status and change nothing.
_FAKE_APP = """
import os, sys
from pathlib import Path

state = Path(os.environ["FAKE_LAUNCHD"])
bundle = Path(__file__).resolve().parents[2]
mode, operation = sys.argv[1], sys.argv[2]
with open(state / "app", "a") as log:
    log.write(mode + " " + operation + "\\n")
control = state / (mode.strip("-") + "_exit")
if control.exists():
    sys.exit(int(control.read_text()))
if mode == "--update-swap":
    previous = bundle.with_name("CORSIA.app.previous")
    os.rename(bundle, previous)
    os.rename(bundle.with_name("CORSIA.app.staged"), bundle)
    print(previous)
elif mode == "--update-rollback":
    previous = bundle.with_name("CORSIA.app.previous")
    temporary = bundle.with_name("CORSIA.app.rollback")
    os.rename(bundle, temporary)
    os.rename(previous, bundle)
    os.rename(temporary, previous)
"""


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


def _python_tool(path: Path, body: str) -> None:
    _executable(path, f"#!{sys.executable}\n{body}")


def _set_version(bundle: Path, short: str, build: str) -> None:
    (bundle / "Contents" / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": "ai.corsia.app",
        "CFBundleShortVersionString": short,
        "CFBundleVersion": build,
    }))


def _build_app(bundle: Path) -> None:
    contents = bundle / "Contents"
    resources = contents / "Resources"
    contents.mkdir(parents=True)
    _set_version(bundle, "1.0.0", "1")
    agents = contents / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    shutil.copy(REPO / "desktop" / "launchd" / f"{LABEL}.plist", agents)
    _python_tool(contents / "MacOS" / "CORSIA", _FAKE_APP)
    scripts = resources / "scripts"
    shutil.copytree(REPO / "scripts" / "corsia.d", scripts / "corsia.d")
    shutil.copy(REPO / "scripts" / "corsia", scripts / "corsia")
    infra = resources / "backend" / "infra"
    infra.mkdir(parents=True)
    (infra / "__init__.py").write_text("")
    for module in ("platform_paths.py", "service_file.py", "fs_durability.py"):
        shutil.copy(BACKEND / "infra" / module, infra / module)
    _executable(
        resources / "python" / "bin" / "python3",
        f'#!/bin/sh\nexec "{sys.executable}" "$@"\n',
    )
    # Anything a source checkout would read here must be ignored.
    (resources / ".env").write_text('echo "ENV FILE SOURCED"\nBACKEND_PORT=1\n')


class App:
    def __init__(self, root: Path, sleepers: list[subprocess.Popen]):
        self.root = root
        self.bundle = root / "Applications" / "CORSIA.app"
        self.home = root / "home"
        self.launchd = root / "launchd"
        self.fakebin = root / "fakebin"
        _build_app(self.bundle)
        self.launchd.mkdir()
        (self.launchd / "starts").write_text("0")
        # launchd's PIDs must be live Python processes, as uvicorn's are.
        (self.launchd / "pids").write_text(" ".join(str(p.pid) for p in sleepers))
        (self.launchd / "pid").write_text(str(sleepers[0].pid))
        _python_tool(self.fakebin / "launchctl", _FAKE_LAUNCHCTL)
        _python_tool(self.fakebin / "curl", _FAKE_CURL)
        _python_tool(self.fakebin / "lsof", _FAKE_LSOF)
        _python_tool(self.fakebin / "ps", _FAKE_PS)
        _executable(self.fakebin / "python3", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        app_dir = self.home / "Library" / "Application Support" / "CORSIA"
        app_dir.mkdir(parents=True)
        workspace = root / "workspace"
        workspace.mkdir()
        (app_dir / "service.json").write_text(
            f'{{"workspace": "{workspace}", "port": {PORT}}}'
        )

    @property
    def script(self) -> Path:
        return self.bundle / "Contents" / "Resources" / "scripts" / "corsia"

    @property
    def plist(self) -> str:
        return str(self.bundle / "Contents" / "Library" / "LaunchAgents" / f"{LABEL}.plist")

    def env(self, **extra: str) -> dict[str, str]:
        return {
            "PATH": f"{self.fakebin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(self.home),
            "FAKE_LAUNCHD": str(self.launchd),
            # The backend hands its caller to every lifecycle run.
            "CORSIA_RESTART_CALLER_TOKEN": "test-caller",
            "CORSIA_HEALTH_WAIT_SECONDS": "5",
            "CORSIA_DRAIN_MAX_WAIT": "1",
            **extra,
        }

    def run(self, *args: str, script: Path | None = None, **extra: str):
        return subprocess.run(
            [str(script or self.script), *args],
            env=self.env(**extra),
            capture_output=True,
            text=True,
            timeout=120,
        )

    def calls(self, name: str = "calls") -> list[str]:
        path = self.launchd / name
        return path.read_text().splitlines() if path.exists() else []

    def launchd_calls(self) -> list[str]:
        """The effects asked of launchd, without the read-only `print`s."""
        return [call for call in self.calls() if not call.startswith("print ")]

    def generation(self, bundle: Path | None = None) -> str:
        info = plistlib.loads(((bundle or self.bundle) / "Contents" / "Info.plist").read_bytes())
        return f"{info['CFBundleShortVersionString']}+{info['CFBundleVersion']}"

    def load(self) -> None:
        result = self.run("start")
        assert result.returncode == 0, result.stdout + result.stderr
        (self.launchd / "calls").write_text("")

    def stage(self, short: str, build: str) -> Path:
        staged = self.bundle.with_name("CORSIA.app.staged")
        shutil.copytree(self.bundle, staged, symlinks=True)
        _set_version(staged, short, build)
        script = staged / "Contents" / "Resources" / "scripts" / "corsia"
        lines = script.read_text().split("\n")
        lines.insert(1, f'echo "lifecycle from {short}+{build}"')
        script.write_text("\n".join(lines))
        return staged


@pytest.fixture
def app(tmp_path):
    sleepers = [
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        for _ in range(2)
    ]
    try:
        yield App(tmp_path, sleepers)
    finally:
        for sleeper in sleepers:
            sleeper.kill()
            sleeper.wait()
        for path in [tmp_path, *tmp_path.rglob("*")]:
            if path.is_dir() and not path.is_symlink():
                path.chmod(path.stat().st_mode | stat.S_IWUSR)


def _snapshot(root: Path) -> list[tuple[str, int, int, int]]:
    return sorted(
        (str(path.relative_to(root)), info.st_mode, info.st_size, info.st_mtime_ns)
        for path in [root, *root.rglob("*")]
        for info in [path.lstat()]
    )


def _make_read_only(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        if not path.is_symlink():
            path.chmod(path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def test_app_mode_uses_its_bundle_service_and_service_json_never_a_checkout(app):
    result = app.run("start", CORSIA_DIR=str(app.root / "elsewhere"),
                     CORSIA_ENV_FILE=str(app.bundle / "Contents" / "Resources" / ".env"))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ENV FILE SOURCED" not in result.stdout + result.stderr
    assert app.launchd_calls() == [f"bootstrap gui/{os.getuid()} {app.plist}"]
    assert f"http://127.0.0.1:{PORT}/api/health" in app.calls("curl")
    assert not (app.home / "Library" / "LaunchAgents").exists()


@pytest.mark.parametrize(
    "service_json",
    ['{"state_dir": "relative/path"}', "{not json"],
    ids=["invalid-state-dir", "unreadable"],
)
def test_app_mode_refuses_a_service_json_it_cannot_honour_instead_of_using_the_default_root(app, service_json):
    """The launcher and the app refuse these; the lifecycle CLI must not fall back to an empty root."""
    service_file = app.home / "Library" / "Application Support" / "CORSIA" / "service.json"
    service_file.write_text(service_json)

    result = app.run("stop")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "no other state folder was opened" in result.stderr
    assert app.calls() == []
    # An explicit state root from the caller is the caller's choice, as for the launcher.
    explicit = app.run("stop", CORSIA_STATE_DIR=str(app.root / "explicit-state"))
    assert "no other state folder was opened" not in explicit.stderr


def test_the_same_scripts_outside_an_app_bundle_are_a_checkout(app):
    checkout = app.root / "checkout" / "scripts"
    shutil.copytree(app.script.parent, checkout)

    result = app.run("start", script=checkout / "corsia")

    assert result.returncode == 1
    assert "noncanonical checkout" in result.stderr
    assert app.calls() == []


def test_start_stop_and_restart_never_write_inside_a_read_only_bundle(app):
    _make_read_only(app.bundle)
    before = _snapshot(app.bundle)

    results = [app.run(verb) for verb in ("start", "stop", "start", "restart")]

    for result in results:
        assert result.returncode == 0, result.stdout + result.stderr
    domain = f"gui/{os.getuid()}"
    assert app.launchd_calls() == [
        f"bootstrap {domain} {app.plist}",
        f"bootout --wait {domain}/{LABEL}",
        f"bootstrap {domain} {app.plist}",
        f"kickstart -k {domain}/{LABEL}",
    ]
    # The restart proved a new process and printed its version identity.
    assert (app.launchd / "starts").read_text() == "3"
    assert f"launchd {LABEL}" in results[-1].stdout
    assert _snapshot(app.bundle) == before


@pytest.mark.parametrize("override", [False, True])
def test_logs_read_the_file_the_launcher_writes(app, override):
    _executable(app.fakebin / "tail", '#!/bin/sh\necho "$@"\n')
    state = app.root / "state" if override else (
        app.home / "Library" / "Application Support" / "CORSIA" / "state"
    )
    extra = {"CORSIA_STATE_DIR": str(state)} if override else {}

    result = app.run("logs", **extra)

    assert result.returncode == 0, result.stdout + result.stderr
    # The launcher's service_log_path(): <state root>/logs/service.log.
    assert result.stdout.strip() == f"-f {state / 'logs' / 'service.log'}"


@pytest.mark.parametrize("foreign", ["another_copy", "another_version"])
def test_every_verb_refuses_a_service_this_app_does_not_own(app, foreign):
    app.load()
    app.stage("2.0.0", "2")
    if foreign == "another_copy":
        other = app.root / "Downloads" / "CORSIA.app"
        (app.launchd / "loaded").write_text(
            f"{other}/Contents/Library/LaunchAgents/{LABEL}.plist"
        )
    else:
        (app.launchd / "generation").write_text("0.9.0+0")

    results = [app.run(*verb) for verb in (["restart"], ["stop"], ["restart-prepared"])]

    for result in results:
        assert result.returncode != 0
    expected = "another copy of CORSIA" if foreign == "another_copy" else "version 0.9.0+0"
    assert expected in results[0].stdout
    assert app.launchd_calls() == []


def test_registration_belongs_to_the_app_binary(app):
    for verb in ("install", "uninstall"):
        result = app.run("service", verb)

        assert result.returncode == 1
        assert "--service register|unregister" in result.stdout
    assert app.launchd_calls() == []
    assert not (app.home / "Library" / "LaunchAgents").exists()


def test_prepare_discard_and_ensure_use_the_app_binary_and_its_staged_copy(app):
    (app.launchd / "update-stage_exit").write_text("41")

    failed = app.run("prepare-redeploy", CORSIA_RESTART_OPERATION_ID=OPERATION_ID)

    assert failed.returncode == 41
    (app.launchd / "update-stage_exit").unlink()
    assert app.run("prepare-redeploy", CORSIA_RESTART_OPERATION_ID=OPERATION_ID).returncode == 0
    assert app.calls("app") == [f"--update-stage {OPERATION_ID}"] * 2
    staged = app.stage("2.0.0", "2")
    assert app.run("discard-prepared").returncode == 0
    assert not staged.exists()
    assert app.run("ensure-current-release").returncode == 0
    assert app.launchd_calls() == []


def test_update_swaps_the_staged_app_and_hands_off_to_its_own_script(app):
    app.load()
    app.stage("2.0.0", "2")

    # Exactly as the backend launches it: the detached envelope sources the
    # installed app's script, which must re-source the new one after the swap.
    result = subprocess.run(
        ["/bin/bash", "-c", LIFECYCLE_SHELL_COMMAND, "corsia-lifecycle",
         str(app.script), "restart-prepared"],
        env=app.env(
            CORSIA_RESTART_BACKEND_PORT=str(PORT),
            CORSIA_RESTART_OPERATION_ID=OPERATION_ID,
            CORSIA_RESTART_CURL=str(app.fakebin / "curl"),
        ),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "lifecycle from 2.0.0+2" in result.stdout
    domain = f"gui/{os.getuid()}"
    assert app.launchd_calls() == [
        f"bootout --wait {domain}/{LABEL}",
        f"bootstrap {domain} {app.plist}",
    ]
    assert app.calls("app") == [f"--update-swap {OPERATION_ID}"]
    assert app.generation() == "2.0.0+2"
    assert (app.launchd / "generation").read_text() == "2.0.0+2"
    assert not app.bundle.with_name("CORSIA.app.staged").exists()
    assert not app.bundle.with_name("CORSIA.app.previous").exists()


def test_a_failed_swap_starts_the_installed_app_again(app):
    app.load()
    staged = app.stage("2.0.0", "2")
    (app.launchd / "update-swap_exit").write_text("42")

    result = app.run("restart-prepared", CORSIA_RESTART_OPERATION_ID=OPERATION_ID)

    assert result.returncode != 0
    assert "could not be installed (swap exit 42)" in result.stdout
    assert "lifecycle from" not in result.stdout
    domain = f"gui/{os.getuid()}"
    assert app.launchd_calls() == [
        f"bootout --wait {domain}/{LABEL}",
        f"bootstrap {domain} {app.plist}",
    ]
    assert app.generation() == "1.0.0+1"
    assert (app.launchd / "generation").read_text() == "1.0.0+1"
    assert staged.exists()


def test_after_the_swap_a_health_failure_rolls_back_and_starts_the_old_app(app):
    app.load()
    app.stage("2.0.0", "2")
    (app.launchd / "health_fail_generation").write_text("2.0.0+2")

    result = app.run("restart-prepared", CORSIA_RESTART_OPERATION_ID=OPERATION_ID)

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "did not become healthy" in output
    assert "went back to version 1.0.0+1" in output
    domain = f"gui/{os.getuid()}"
    assert app.launchd_calls() == [
        f"bootout --wait {domain}/{LABEL}",
        f"bootstrap {domain} {app.plist}",
        f"bootout --wait {domain}/{LABEL}",
        f"bootstrap {domain} {app.plist}",
    ]
    assert app.calls("app") == [
        f"--update-swap {OPERATION_ID}",
        f"--update-rollback {OPERATION_ID}",
    ]
    assert app.generation() == "1.0.0+1"
    assert (app.launchd / "generation").read_text() == "1.0.0+1"
    assert not app.bundle.with_name("CORSIA.app.previous").exists()


def test_after_the_swap_a_wrong_version_rolls_back_and_starts_the_old_app(app):
    previous = app.bundle.with_name("CORSIA.app.previous")
    shutil.copytree(app.bundle, previous, symlinks=True)
    _set_version(app.bundle, "2.0.0", "2")
    # The backend that answers is not the one this app ships.
    app.fakebin.joinpath("launchctl").write_text(
        app.fakebin.joinpath("launchctl").read_text()
        + '\nif command in ("bootstrap", "kickstart"):\n'
        + '    (state / "generation").write_text("1.0.0+1")\n'
    )

    result = app.run("restart-prepared", CORSIA_RESTART_OPERATION_ID=OPERATION_ID)

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "is version 1.0.0+1, not 2.0.0+2" in output
    assert "went back to version 1.0.0+1" in output
    assert app.calls("app") == [f"--update-rollback {OPERATION_ID}"]
    assert app.generation() == "1.0.0+1"
    assert (app.launchd / "generation").read_text() == "1.0.0+1"
    assert not previous.exists()
