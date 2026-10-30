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
APP_SCHEMA_VERSION = 5

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
    if (state / "fail_bootout").exists():
        sys.exit(5)
    if not loaded.exists():
        sys.exit(3)
    loaded.unlink()
    # launchd ends the job's process before bootout is done with it.
    try:
        os.kill(int((state / "pid").read_text()), 9)
    except ProcessLookupError:
        pass
    # A migration that commits as that generation's process is stopped.
    migrated = state / "bootout_migrates"
    if migrated.exists() and migrated.read_text().split()[2] == (state / "generation").read_text():
        import sqlite3
        db, version, _generation = migrated.read_text().split()
        conn = sqlite3.connect(db)
        conn.execute(f"PRAGMA user_version={int(version)}")
        conn.commit()
        conn.close()
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
if "POST" in sys.argv:
    with open(state / "curl_post", "a") as log:
        log.write(url.split("?")[0] + "\\n")
serving = (state / "loaded").exists()
# A drain the shell already owns: quiet at once, ready on request, and its
# release recorded with the operation id the cancel names.
base = url.split("?")[0]
operation = os.environ.get("CORSIA_RESTART_OPERATION_ID", "")
if base.endswith("/api/system/drain") and serving and operation:
    if "DELETE" in sys.argv:
        (state / "released_drain").write_text(url.split("operation_id=")[-1])
    else:
        print(json.dumps({"active": True, "operation_id": operation, "activeProcesses": 0}))
    sys.exit(0)
if base.endswith("/api/system/drain/ready") and serving and operation:
    print(json.dumps({"ok": True, "operation_id": operation}) + "\\n200")
    sys.exit(0)
if url.endswith("/api/health") and serving:
    failed_generation = state / "health_fail_generation"
    if failed_generation.exists() and (state / "generation").read_text() == failed_generation.read_text():
        sys.exit(22)
    sys.exit(0)
if "/api/system/drain/update?" in url and serving:
    body = json.loads(sys.argv[sys.argv.index("-d") + 1])
    if (state / "failure_report_exit").exists() or (body["healthy"] and (state / "settle_operation_exit").exists()):
        sys.exit(22)
    outcome = "complete" if body["healthy"] else "failed"
    (state / ("settled_operation" if body["healthy"] else "failed_operation")).write_text(body["operation_id"])
    print(json.dumps({"operation_id": body["operation_id"], "status": outcome}))
    sys.exit(0)
if url.endswith("/api/system/stop-local-model") and serving:
    sys.exit(22 if (state / "fail_stop_local_model").exists() else 0)
if url.endswith("/api/version") and serving:
    # Whether the lifecycle shell holds the update lock while it proves a version.
    if os.environ.get("CORSIA_STATE_DIR"):
        import fcntl
        lock_path = Path(os.environ["CORSIA_STATE_DIR"]) / "update.lock"
        busy = False
        if lock_path.exists():
            with open(lock_path, "rb") as probe:
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    busy = True
        with open(state / "version_lock", "a") as log:
            log.write(f"held={busy}\\n")
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
import os, plistlib, sys
from pathlib import Path

state = Path(os.environ["FAKE_LAUNCHD"])
bundle = Path(__file__).resolve().parents[2]
mode, operation = sys.argv[1], sys.argv[2]
with open(state / "app", "a") as log:
    log.write(mode + " " + operation + "\\n")
# What this call saw of the update lock: the descriptor it was told about,
# whether it holds descriptor 7 at all, and whether an opener of its own is
# refused because the lifecycle shell holds the lock.
if (mode in {"--update-swap", "--update-rollback"} or operation == "register") and os.environ.get("CORSIA_STATE_DIR"):
    import fcntl
    lock_path = Path(os.environ["CORSIA_STATE_DIR"]) / "update.lock"
    try:
        os.fstat(7)
        fd7 = "open"
    except OSError:
        fd7 = "closed"
    busy = False
    if lock_path.exists():
        with open(lock_path, "rb") as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                busy = True
    with open(state / "lock", "a") as log:
        told = os.environ.get("CORSIA_UPDATE_LOCK_FD", "-")
        log.write(f"{mode} {operation} told={told} fd7={fd7} held={busy}\\n")
control = state / (mode.strip("-") + "_exit")
if control.exists():
    sys.exit(int(control.read_text()))
if mode == "--service" and operation not in {"register", "unregister", "status"}:
    sys.exit(64)
if mode == "--service" and operation == "unregister":
    loaded = state / "loaded"
    if loaded.exists():
        loaded.unlink()
elif mode == "--service" and operation == "register":
    plist = bundle / "Contents" / "Library" / "LaunchAgents" / "ai.corsia.service.plist"
    state.joinpath("loaded").write_text(str(plist))
    info = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
    generation = info["CFBundleShortVersionString"] + "+" + info["CFBundleVersion"]
    forced = state / "service-register_generation"
    state.joinpath("generation").write_text(forced.read_text() if forced.exists() else generation)
    starts = int(state.joinpath("starts").read_text()) + 1
    state.joinpath("starts").write_text(str(starts))
    pids = state.joinpath("pids").read_text().split()
    state.joinpath("pid").write_text(pids[starts % len(pids)])
elif mode == "--update-swap":
    previous = bundle.with_name("CORSIA.app.previous")
    os.rename(bundle, previous)
    os.rename(bundle.with_name("CORSIA.app.staged"), bundle)
    print(previous)
elif mode == "--update-rollback":
    (state / "rollback_reported").write_text(str((state / "failed_operation").exists()))
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
    # Read by the rollback's schema check, which never imports it.
    (infra / "schema.py").write_text(f"SCHEMA_VERSION = {APP_SCHEMA_VERSION}\n")
    _python_tool(
        resources / "python" / "bin" / "python3",
        """
import os, sys
from pathlib import Path

os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
""",
    )
    # Anything a source checkout would read here must be ignored.
    (resources / ".env").write_text('echo "ENV FILE SOURCED"\nBACKEND_PORT=1\n')


class App:
    def __init__(self, root: Path, sleepers: list[int]):
        self.root = root
        self.bundle = root / "Applications" / "CORSIA.app"
        self.home = root / "home"
        self.launchd = root / "launchd"
        self.fakebin = root / "fakebin"
        _build_app(self.bundle)
        self.launchd.mkdir()
        (self.launchd / "starts").write_text("0")
        # launchd's PIDs must be live Python processes, as uvicorn's are.
        (self.launchd / "pids").write_text(" ".join(str(pid) for pid in sleepers))
        (self.launchd / "pid").write_text(str(sleepers[0]))
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
        # An installed app has data at the schema it ships.
        _database_at(app_dir / "state", APP_SCHEMA_VERSION)

    @property
    def script(self) -> Path:
        return self.bundle / "Contents" / "Resources" / "scripts" / "corsia"

    @property
    def update_lock(self) -> Path:
        return self.home / "Library" / "Application Support" / "CORSIA" / "state" / "update.lock"

    def update_lock_is_free(self) -> bool:
        import fcntl

        with open(self.update_lock, "rb") as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
        return True

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
            # The drain loop reads a whole-second clock, so a 1-second cap can expire between its start and
            # its first poll on a slow runner. No test here needs the cap to expire; a quiet drain returns at once.
            "CORSIA_DRAIN_MAX_WAIT": "30",
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
        (self.launchd / "app").write_text("")
        (self.launchd / "lock").unlink(missing_ok=True)
        (self.launchd / "curl_post").unlink(missing_ok=True)
        (self.launchd / "version_lock").unlink(missing_ok=True)

    def stage(self, short: str, build: str) -> Path:
        staged = self.bundle.with_name("CORSIA.app.staged")
        shutil.copytree(self.bundle, staged, symlinks=True)
        _set_version(staged, short, build)
        script = staged / "Contents" / "Resources" / "scripts" / "corsia"
        lines = script.read_text().split("\n")
        lines.insert(1, f'echo "lifecycle from {short}+{build}"')
        script.write_text("\n".join(lines))
        return staged


@pytest.mark.parametrize("args", [(), ("help",), ("--help",), ("-h",)])
def test_packaged_cli_help_lists_app_commands(app, args):
    entry = app.bundle / "Contents" / "Resources" / "bin" / "corsia"
    entry.parent.mkdir(parents=True)
    shutil.copy(REPO / "desktop" / "scripts" / "corsia", entry)
    result = app.run(*args, script=entry)
    assert result.returncode == 0, result.stderr
    for verb in ("start", "open", "stop", "restart", "status", "logs", "service",
                 "init", "setup", "bind-host", "remote-address", "event", "artifact", "mcp"):
        assert verb in result.stdout
    for refused in ("redeploy", "install", "uninstall"):
        assert refused not in result.stdout
    assert not app.launchd_calls()


@pytest.fixture
def app(tmp_path):
    # Orphaned at once, so launchd (PID 1) reaps one that bootout stops, as it
    # does for a service; a pytest child would linger as a zombie instead.
    sleepers = [
        int(subprocess.run(
            ["/bin/sh", "-c", f'"{sys.executable}" -c "import time; time.sleep(600)" '
             "</dev/null >/dev/null 2>&1 & echo $!"],
            check=True, capture_output=True, text=True,
        ).stdout)
        for _ in range(2)
    ]
    try:
        yield App(tmp_path, sleepers)
    finally:
        for sleeper in sleepers:
            try:
                os.kill(sleeper, 9)
            except ProcessLookupError:
                pass
        for path in [tmp_path, *tmp_path.rglob("*")]:
            if path.is_dir() and not path.is_symlink():
                path.chmod(path.stat().st_mode | stat.S_IWUSR)


def _snapshot(root: Path) -> list[tuple[str, int, int, int]]:
    return sorted(
        (str(path.relative_to(root)), info.st_mode, info.st_size, info.st_mtime_ns)
        for path in [root, *root.rglob("*")]
        for info in [path.lstat()]
    )


def test_app_mode_keeps_bundled_tools_ahead_of_service_defaults(app):
    bundled_bin = app.bundle / "Contents" / "Resources" / "bin"
    service_bin = app.home / ".local" / "bin"
    _executable(bundled_bin / "node", "#!/bin/sh\nprintf 'bundled-node\\n'\n")
    _executable(bundled_bin / "npm", "#!/usr/bin/env node\n")
    for name in ("node", "npm"):
        _executable(service_bin / name, "#!/bin/sh\nprintf 'service-tool\\n'\n")
    delegate = app.script.parent / "corsia.d" / "delegate.sh"
    delegate.write_text(
        delegate.read_text()
        + "\nis_app_mode || exit 1\ncommand -v npm\ncommand -v node\nnpm --version\nexit 0\n"
    )

    result = app.run("start", PATH=f"{bundled_bin}:{app.env()['PATH']}")

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        str(bundled_bin / "npm"), str(bundled_bin / "node"), "bundled-node"
    ]
    assert app.launchd_calls() == []


def _make_read_only(root: Path) -> None:
    for path in [root, *root.rglob("*")]:
        if not path.is_symlink():
            path.chmod(path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def test_app_mode_uses_its_bundle_service_and_service_json_never_a_checkout(app):
    result = app.run("start", CORSIA_DIR=str(app.root / "elsewhere"),
                     CORSIA_ENV_FILE=str(app.bundle / "Contents" / "Resources" / ".env"))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ENV FILE SOURCED" not in result.stdout + result.stderr
    assert app.launchd_calls() == []
    assert app.calls("app") == ["--service unregister", "--service register"]
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


def test_app_mode_refuses_to_start_while_the_app_moves_the_data_folder(app):
    """T9: only stopping and reading run while CORSIA/state-move.json exists."""
    journal = app.home / "Library" / "Application Support" / "CORSIA" / "state-move.json"
    journal.write_text('{"step": "moved"}')

    for verb in (["start"], ["restart"], ["service", "start"], ["restart-prepared"]):
        result = app.run(*verb, CORSIA_RESTART_OPERATION_ID=OPERATION_ID)
        assert result.returncode != 0, verb
        assert "the app is moving CORSIA's data." in result.stderr, verb
    assert app.launchd_calls() == []
    assert app.calls("app") == []

    stopped = app.run("stop")
    assert "moving CORSIA's data" not in stopped.stderr


def test_app_mode_stop_can_stop_the_local_model_before_bootout(app):
    app.load()

    result = app.run("stop", "--stop-local-model")

    assert result.returncode == 0, result.stdout + result.stderr
    assert app.calls("curl_post")[-1].endswith("/api/system/stop-local-model")
    assert app.launchd_calls() == [f"bootout gui/{os.getuid()}/{LABEL}"]


def test_app_mode_stop_keeps_the_service_when_the_local_model_will_not_stop(app):
    app.load()
    (app.launchd / "fail_stop_local_model").write_text("")

    result = app.run("stop", "--stop-local-model", CORSIA_RESTART_OPERATION_ID=OPERATION_ID)

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Drain complete" in output
    assert "Could not stop the local model" in output
    # The drain hold is released, so the service that stays up takes turns again.
    assert (app.launchd / "released_drain").read_text() == OPERATION_ID
    assert "Could not cancel drain mode" not in output
    assert app.launchd_calls() == []
    assert (app.launchd / "loaded").exists()


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
        f"bootout {domain}/{LABEL}",
        f"kickstart -k {domain}/{LABEL}",
    ]
    assert app.calls("app") == [
        "--service unregister",
        "--service register",
        "--service unregister",
        "--service register",
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


def test_direct_app_script_refuses_source_redeploy(app):
    result = app.run("redeploy")

    assert result.returncode != 0
    assert "source checkout" in result.stdout
    assert app.launchd_calls() == []


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

    assert result.returncode == 0, result.stdout + result.stderr + str(app.calls("curl"))
    assert "lifecycle from 2.0.0+2" in result.stdout
    domain = f"gui/{os.getuid()}"
    assert app.launchd_calls() == [f"bootout {domain}/{LABEL}"]
    assert app.calls("app") == [
        f"--update-swap {OPERATION_ID}",
        "--service unregister",
        "--service register",
    ]
    assert app.generation() == "2.0.0+2"
    assert (app.launchd / "generation").read_text() == "2.0.0+2"
    assert (app.launchd / "settled_operation").read_text() == OPERATION_ID
    assert not app.bundle.with_name("CORSIA.app.staged").exists()
    assert not app.bundle.with_name("CORSIA.app.previous").exists()
    # One lock from before the stop through the new app's start: the swap
    # reuses the shell's descriptor, and the start is never handed it.
    assert app.calls("lock") == [
        f"--update-swap {OPERATION_ID} told=7 fd7=open held=True",
        "--service register told=- fd7=closed held=True",
    ]
    # Still held while the new version is proven: once taken, every later
    # version check runs under it (the old app's read before the stop need not).
    checks = app.calls("version_lock")
    assert "held=True" in checks and set(checks[checks.index("held=True"):]) == {"held=True"}, checks
    assert app.update_lock_is_free()


def test_the_update_refuses_before_any_effect_while_another_holds_the_update_lock(app):
    app.load()
    staged = app.stage("2.0.0", "2")
    app.update_lock.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [sys.executable, "-c", "import fcntl, sys, time\n"
         "f = open(sys.argv[1], 'a')\nfcntl.flock(f, fcntl.LOCK_EX)\nprint('held', flush=True)\ntime.sleep(60)",
         str(app.update_lock)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        result = app.run("restart-prepared", CORSIA_RESTART_OPERATION_ID=OPERATION_ID)
    finally:
        holder.kill()
        holder.wait()

    assert result.returncode != 0
    assert "another update or start of CORSIA is in progress" in result.stdout
    assert app.launchd_calls() == []
    assert app.calls("app") == []
    assert not any("/drain" in call for call in app.calls("curl_post"))
    assert staged.is_dir() and app.generation() == "1.0.0+1"
    assert (app.launchd / "loaded").exists()


def test_a_descriptor_7_that_is_not_the_update_lock_is_replaced_never_trusted(app):
    app.load()
    app.stage("2.0.0", "2")
    other = app.root / "other.lock"
    other.write_text("")

    result = subprocess.run(
        ["/bin/bash", "-c", 'exec 7<>"$1"; exec "$2" restart-prepared', "forged", str(other), str(app.script)],
        env=app.env(CORSIA_RESTART_OPERATION_ID=OPERATION_ID),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    # Run directly, outside the backend's envelope, the handoff execs the new
    # app's script, and the lock still carries through it to the new start.
    assert app.calls("lock") == [
        f"--update-swap {OPERATION_ID} told=7 fd7=open held=True",
        "--service register told=- fd7=closed held=True",
    ]
    assert app.update_lock_is_free()


def test_a_completion_failure_rolls_back_the_healthy_swapped_app(app):
    app.load()
    app.stage("2.0.0", "2")
    (app.launchd / "settle_operation_exit").write_text("1")

    result = app.run("restart-prepared", CORSIA_RESTART_OPERATION_ID=OPERATION_ID)

    assert result.returncode != 0
    assert "could not complete its Update operation" in result.stdout + result.stderr
    assert app.generation() == "1.0.0+1"
    assert not (app.launchd / "settled_operation").exists()
    assert app.calls("app") == [
        f"--update-swap {OPERATION_ID}",
        "--service unregister",
        "--service register",
        f"--update-rollback {OPERATION_ID}",
        "--service unregister",
        "--service register",
    ]
    # The exec'd new script keeps the same lock through the rollback's proof.
    assert app.calls("lock") == [
        f"--update-swap {OPERATION_ID} told=7 fd7=open held=True",
        "--service register told=- fd7=closed held=True",
        f"--update-rollback {OPERATION_ID} told=7 fd7=open held=True",
        "--service register told=- fd7=closed held=True",
    ]
    assert app.update_lock_is_free()


def test_failed_rollback_stop_leaves_an_acknowledged_failure_and_the_new_service(app):
    previous = app.bundle.with_name("CORSIA.app.previous")
    shutil.copytree(app.bundle, previous, symlinks=True)
    _set_version(app.bundle, "2.0.0", "2")
    app.load()
    (app.launchd / "health_fail_generation").write_text("2.0.0+2")
    (app.launchd / "fail_bootout").touch()

    result = app.run("restart-prepared", CORSIA_RESTART_OPERATION_ID=OPERATION_ID)

    assert result.returncode != 0
    assert "could not stop the new service" in result.stdout
    assert (app.launchd / "failed_operation").read_text() == OPERATION_ID
    assert (app.launchd / "loaded").exists()
    assert previous.is_dir() and app.generation() == "2.0.0+2"


def test_unacknowledged_failure_keeps_the_running_service_in_place(app):
    previous = app.bundle.with_name("CORSIA.app.previous")
    shutil.copytree(app.bundle, previous, symlinks=True)
    _set_version(app.bundle, "2.0.0", "2")
    app.load()
    (app.launchd / "failure_report_exit").touch()

    result = app.run("restart-prepared", CORSIA_RESTART_OPERATION_ID=OPERATION_ID)

    assert result.returncode != 0
    assert "failure could not be recorded" in result.stdout
    assert not (app.launchd / "failed_operation").exists()
    assert not any("--update-rollback" in call for call in app.calls("app"))
    assert previous.is_dir() and app.generation() == "2.0.0+2"


def test_after_a_handoff_without_the_lock_the_new_app_takes_its_own(app):
    # An app from before the lock hands off with descriptor 7 closed.
    previous = app.bundle.with_name("CORSIA.app.previous")
    shutil.copytree(app.bundle, previous, symlinks=True)
    _set_version(app.bundle, "2.0.0", "2")
    app.load()

    result = app.run("restart-prepared", CORSIA_RESTART_OPERATION_ID=OPERATION_ID)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "update lock could not be held" not in result.stdout + result.stderr
    assert not previous.exists()
    assert app.calls("version_lock") and set(app.calls("version_lock")) == {"held=True"}
    assert app.update_lock_is_free()


def test_after_the_swap_another_holder_of_the_lock_stops_the_update_before_any_effect(app):
    previous = app.bundle.with_name("CORSIA.app.previous")
    shutil.copytree(app.bundle, previous, symlinks=True)
    _set_version(app.bundle, "2.0.0", "2")
    app.load()
    app.update_lock.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [sys.executable, "-c", "import fcntl, sys, time\n"
         "f = open(sys.argv[1], 'a')\nfcntl.flock(f, fcntl.LOCK_EX)\nprint('held', flush=True)\ntime.sleep(60)",
         str(app.update_lock)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        result = app.run("restart-prepared", CORSIA_RESTART_OPERATION_ID=OPERATION_ID)
    finally:
        holder.kill()
        holder.wait()

    assert result.returncode != 0
    assert "another update or start of CORSIA is in progress" in result.stdout
    assert app.launchd_calls() == []
    assert app.calls("app") == []
    assert app.calls("curl_post") == []
    assert previous.is_dir() and app.generation() == "2.0.0+2"
    assert (app.launchd / "loaded").exists()


def test_a_failed_swap_starts_the_installed_app_again(app):
    app.load()
    staged = app.stage("2.0.0", "2")
    (app.launchd / "update-swap_exit").write_text("42")

    result = app.run("restart-prepared", CORSIA_RESTART_OPERATION_ID=OPERATION_ID)

    assert result.returncode != 0
    assert "could not be installed (swap exit 42)" in result.stdout
    assert "lifecycle from" not in result.stdout
    domain = f"gui/{os.getuid()}"
    assert app.launchd_calls() == [f"bootout {domain}/{LABEL}"]
    assert app.calls("app") == [
        f"--update-swap {OPERATION_ID}",
        "--service unregister",
        "--service register",
    ]
    assert app.generation() == "1.0.0+1"
    assert (app.launchd / "generation").read_text() == "1.0.0+1"
    assert staged.exists()


def test_after_the_swap_a_health_failure_rolls_back_and_starts_the_old_app(app):
    app.load()
    app.stage("2.0.0", "2")
    # Both app generations emulate build 417's register/unregister-only service CLI.
    (app.launchd / "health_fail_generation").write_text("2.0.0+2")

    result = app.run("restart-prepared", CORSIA_RESTART_OPERATION_ID=OPERATION_ID)

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "did not become healthy" in output
    assert "went back to version 1.0.0+1" in output
    assert (app.launchd / "rollback_reported").read_text() == "True"
    domain = f"gui/{os.getuid()}"
    assert app.launchd_calls() == [
        f"bootout {domain}/{LABEL}",
        f"bootout {domain}/{LABEL}",
    ]
    assert app.calls("app") == [
        f"--update-swap {OPERATION_ID}",
        "--service unregister",
        "--service register",
        f"--update-rollback {OPERATION_ID}",
        "--service unregister",
        "--service register",
    ]
    assert app.generation() == "1.0.0+1"
    assert (app.launchd / "generation").read_text() == "1.0.0+1"
    assert not app.bundle.with_name("CORSIA.app.previous").exists()


def test_after_the_swap_a_wrong_version_rolls_back_and_starts_the_old_app(app):
    previous = app.bundle.with_name("CORSIA.app.previous")
    shutil.copytree(app.bundle, previous, symlinks=True)
    _set_version(app.bundle, "2.0.0", "2")
    # The backend that answers is not the one this app ships.
    (app.launchd / "service-register_generation").write_text("1.0.0+1")

    result = app.run("restart-prepared", CORSIA_RESTART_OPERATION_ID=OPERATION_ID)

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "is version 1.0.0+1, not 2.0.0+2" in output
    assert "went back to version 1.0.0+1" in output
    assert (app.launchd / "rollback_reported").read_text() == "True"
    domain = f"gui/{os.getuid()}"
    assert app.launchd_calls() == [
        f"bootout {domain}/{LABEL}",
    ]
    assert app.calls("app") == [
        "--service unregister",
        "--service register",
        f"--update-rollback {OPERATION_ID}",
        "--service unregister",
        "--service register",
    ]
    assert app.generation() == "1.0.0+1"
    assert (app.launchd / "generation").read_text() == "1.0.0+1"
    assert not previous.exists()


def _database_at(state: Path, version: int) -> Path:
    import sqlite3

    state.mkdir(parents=True, exist_ok=True)
    db = state / "corsia.db"
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version={version}")
    conn.commit()
    conn.close()
    return db


def test_after_the_swap_data_the_old_app_cannot_open_keeps_the_new_app(app):
    app.load()
    app.stage("2.0.0", "2")
    (app.launchd / "health_fail_generation").write_text("2.0.0+2")
    state = app.root / "state"
    _database_at(state, APP_SCHEMA_VERSION + 1)

    result = app.run("restart-prepared", CORSIA_RESTART_OPERATION_ID=OPERATION_ID, CORSIA_STATE_DIR=str(state))

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "already upgraded CORSIA's data, which version 1.0.0+1 can't open" in output
    assert "went back to version" not in output
    assert not any("--update-rollback" in call for call in app.calls("app"))
    domain = f"gui/{os.getuid()}"
    assert app.launchd_calls() == [f"bootout {domain}/{LABEL}"]
    assert app.generation() == "2.0.0+2"
    assert (app.launchd / "generation").read_text() == "2.0.0+2"
    assert (app.launchd / "failed_operation").read_text() == OPERATION_ID
    assert app.bundle.with_name("CORSIA.app.previous").is_dir()


def test_a_stale_state_path_with_no_database_keeps_the_new_app(app):
    """T10: a state path whose data moved away can't prove a downgrade is safe."""
    app.load()
    app.stage("2.0.0", "2")
    (app.launchd / "health_fail_generation").write_text("2.0.0+2")
    moved_away = app.root / "com.cebertowicz.corsia"

    result = app.run("restart-prepared", CORSIA_RESTART_OPERATION_ID=OPERATION_ID,
                     CORSIA_STATE_DIR=str(moved_away))

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "Could not tell whether" in output
    assert "went back to version" not in output
    assert not any("--update-rollback" in call for call in app.calls("app"))
    assert app.generation() == "2.0.0+2"
    assert not (moved_away / "corsia.db").exists()


def test_a_migration_committed_as_the_rollback_stops_the_service_keeps_the_new_app(app):
    app.load()
    app.stage("2.0.0", "2")
    (app.launchd / "health_fail_generation").write_text("2.0.0+2")
    state = app.root / "state"
    db = _database_at(state, APP_SCHEMA_VERSION)
    (app.launchd / "bootout_migrates").write_text(f"{db} {APP_SCHEMA_VERSION + 1} 2.0.0+2")

    result = app.run("restart-prepared", CORSIA_RESTART_OPERATION_ID=OPERATION_ID, CORSIA_STATE_DIR=str(state))

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "already upgraded CORSIA's data" in output
    assert not any("--update-rollback" in call for call in app.calls("app"))
    domain = f"gui/{os.getuid()}"
    # Stopped once for the swap and once for the rollback, then started again.
    assert app.launchd_calls() == [
        f"bootout {domain}/{LABEL}",
        f"bootout {domain}/{LABEL}",
    ]
    assert app.calls("app")[-2:] == ["--service unregister", "--service register"]
    assert app.generation() == "2.0.0+2"
    assert (app.launchd / "generation").read_text() == "2.0.0+2"


def test_app_mode_stop_says_when_it_cannot_stop_the_local_model(app):
    app.load()

    result = app.run("stop", "--force", "--stop-local-model")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "--stop-local-model skipped: the backend is not answering" in result.stdout + result.stderr
    assert not any(call.endswith("/stop-local-model") for call in app.calls("curl_post"))
    assert app.launchd_calls() == [f"bootout gui/{os.getuid()}/{LABEL}"]
