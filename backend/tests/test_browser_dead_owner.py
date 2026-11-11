"""Dead browser owners are recovered without signalling live or unowned sessions."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from host_access import browser_identity, browser_orphan, browser_owner_lock, browser_ports, browser_session_launch, processes
from tests.browser_process_fixture import synthetic_browser as synthetic_browser

SESSION = "session123456"


class FakeProcess:
    def __init__(self, pid, directory, *, birth=123.0, chrome=False, profile="/tmp/profile", headless=True):
        self.pid = pid
        self.directory = directory
        self.birth = birth
        self.chrome = chrome
        self.profile = profile
        self.headless = headless

    def is_running(self):
        return True

    def ppid(self):
        return 1  # its owning backend is dead, so launchd adopted it

    def create_time(self):
        return self.birth

    def uids(self):
        return SimpleNamespace(real=os.getuid())

    def exe(self):
        return "/usr/bin/agent-browser"

    def environ(self):
        return {"AGENT_BROWSER_DAEMON": "1", "AGENT_BROWSER_SESSION": SESSION,
                "AGENT_BROWSER_SOCKET_DIR": str(self.directory)}

    def cmdline(self):
        return ["/usr/bin/chrome", f"--user-data-dir={self.profile}", *(["--headless=new"] if self.headless else [])]


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    root = tmp_path / "sockets"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(browser_ports, "SOCKET_ROOT", root)
    fake = {}
    signalled = []
    monkeypatch.setattr(browser_identity, "process_for_identity", lambda pid: fake[pid])
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)

    def stop(pid, check, **_kwargs):
        if check() is True:
            signalled.append(pid)
            return "stopped"
        return "unverified"

    monkeypatch.setattr(processes, "stop_orphan_group", stop)
    return root, fake, signalled


def make_dir(root, name, *, locked=True):
    directory = root / name
    directory.mkdir(mode=0o700)
    if locked:
        fd = browser_owner_lock.acquire(directory, create=True)
        browser_owner_lock.release(fd)
    return directory


def test_free_lock_stops_daemon_and_recorded_chrome(recovery, tmp_path):
    root, fake, signalled = recovery
    directory = make_dir(root, "dead")
    profile = tmp_path / "profile"
    profile.mkdir()
    marker = profile / "sign-in"
    marker.write_text("kept")
    (directory / f"{SESSION}.pid").write_text("101")
    browser_owner_lock.record_chrome(directory, SESSION, 102, 123.0, str(profile))
    fake[101] = FakeProcess(101, directory)
    fake[102] = FakeProcess(102, directory, chrome=True, profile=str(profile))
    fake[103] = FakeProcess(103, directory, chrome=True, profile=str(profile))
    browser_orphan.close_dead_sessions(root / "own", root=root)
    assert signalled == [101, 102]
    assert marker.read_text() == "kept"


def test_dead_owner_visible_window_is_left_for_the_person(recovery):
    root, fake, signalled = recovery
    directory = make_dir(root, "dead")
    browser_owner_lock.record_chrome(directory, SESSION, 102, 123.0, "/tmp/profile")
    fake[102] = FakeProcess(102, directory, chrome=True, headless=False)
    browser_orphan.close_dead_sessions(root / "own", root=root)
    assert not signalled


def test_reused_chrome_pid_is_kept(recovery):
    root, fake, signalled = recovery
    directory = make_dir(root, "dead")
    browser_owner_lock.record_chrome(directory, SESSION, 102, 123.0, "/tmp/profile")
    fake[102] = FakeProcess(102, directory, birth=124.0, chrome=True)
    browser_orphan.close_dead_sessions(root / "own", root=root)
    assert not signalled


def test_lock_held_by_another_process_keeps_daemon_and_chrome(recovery):
    root, fake, signalled = recovery
    directory = make_dir(root, "live")
    (directory / f"{SESSION}.pid").write_text("101")
    browser_owner_lock.record_chrome(directory, SESSION, 102, 123.0, "/tmp/profile")
    fake[101] = FakeProcess(101, directory)
    fake[102] = FakeProcess(102, directory, chrome=True)
    script = ("import fcntl,sys; f=open(sys.argv[1], 'r+'); "
              "fcntl.flock(f, fcntl.LOCK_EX); print('ready', flush=True); sys.stdin.read()")
    child = subprocess.Popen([sys.executable, "-c", script, str(directory / "owner.lock")],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        browser_orphan.close_dead_sessions(root / "own", root=root)
        assert not signalled
    finally:
        child.stdin.close()
        child.wait(timeout=5)


def test_own_and_lockless_dirs_are_kept(recovery, tmp_path):
    root, fake, signalled = recovery
    own = make_dir(root, "own")
    old = make_dir(root, "old", locked=False)
    for directory, pid in ((own, 101), (old, 102)):
        (directory / f"{SESSION}.pid").write_text(str(pid))
        fake[pid] = FakeProcess(pid, directory)
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    browser_orphan.close_dead_sessions(alias / "own", root=root)
    assert not signalled


async def test_owner_holds_lock_before_first_launch_and_releases_after_shutdown(synthetic_browser):
    owner = synthetic_browser.owner
    original = synthetic_browser.invoke

    async def checked_invoke(argv, *, timeout):
        assert browser_owner_lock.acquire(owner.socket_dir, create=False) is None
        return await original(argv, timeout=timeout)

    owner._invoke = checked_invoke
    await owner.run("thread", ["snapshot"])
    await owner.shutdown()
    fd = browser_owner_lock.acquire(owner.socket_dir, create=False)
    assert fd is not None
    browser_owner_lock.release(fd)


async def test_owner_retries_lock_after_another_holder_releases_it(tmp_path):
    owner = SimpleNamespace(socket_dir=tmp_path, _lifetime_lock=None)
    held = browser_owner_lock.acquire(tmp_path, create=True)
    assert held is not None
    try:
        with pytest.raises(browser_identity.BrowserIdentityError, match="lock is unavailable"):
            await browser_session_launch._acquire_owner_lock(owner)
        assert owner._lifetime_lock is None
    finally:
        browser_owner_lock.release(held)
    await browser_session_launch._acquire_owner_lock(owner)
    assert owner._lifetime_lock is not None
    browser_owner_lock.release(owner._lifetime_lock)


async def test_cancelled_lock_acquisition_keeps_the_acquired_fd(tmp_path, monkeypatch):
    started = threading.Event()
    finish = threading.Event()
    original = browser_owner_lock.acquire
    owner = SimpleNamespace(socket_dir=tmp_path, _lifetime_lock=None)

    def slow_acquire(directory, *, create):
        started.set()
        assert finish.wait(timeout=5)
        return original(directory, create=create)

    monkeypatch.setattr(browser_owner_lock, "acquire", slow_acquire)
    task = asyncio.create_task(browser_session_launch._acquire_owner_lock(owner))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert owner._lifetime_lock is not None
    assert original(tmp_path, create=False) is None
    browser_owner_lock.release(owner._lifetime_lock)


def test_outside_and_unreadable_daemons_are_kept(recovery, tmp_path, monkeypatch, caplog):
    root, fake, signalled = recovery
    directory = make_dir(root, "dead")
    (directory / f"{SESSION}.pid").write_text("101")
    outside = make_dir(tmp_path, "outside")
    (outside / f"{SESSION}.pid").write_text("102")
    fake[101] = FakeProcess(101, outside)
    fake[102] = FakeProcess(102, outside)
    browser_orphan.close_dead_sessions(root / "own", root=root)
    assert not signalled
    monkeypatch.setattr(fake[101], "environ", lambda: (_ for _ in ()).throw(PermissionError()))
    browser_orphan.close_dead_sessions(root / "own", root=root)
    assert not signalled
    assert "browser orphan could not be verified" in caplog.text
    caplog.clear()

    def absent(_pid):
        raise browser_identity.BrowserPidAbsent

    monkeypatch.setattr(browser_identity, "process_for_identity", absent)
    browser_orphan.close_dead_sessions(root / "own", root=root)
    assert not signalled
    assert "browser orphan could not be verified" not in caplog.text


def test_sweep_logs_what_it_left_unchecked(recovery, monkeypatch, caplog):
    root, fake, signalled = recovery
    make_dir(root, "dead")
    (root / "dead" / "owner.lock").chmod(0o000)
    browser_orphan.close_dead_sessions(root / "own", root=root)
    assert "browser orphan sweep skipped dead: PermissionError" in caplog.text
    caplog.clear()
    clock = iter((0.0, 11.0))
    monkeypatch.setattr(browser_orphan.time, "monotonic", lambda: next(clock))
    browser_orphan.close_dead_sessions(root / "own", root=root)
    assert "time limit; 1 directories not checked" in caplog.text
    assert not signalled


REAL_STOP = processes.stop_orphan_group


@pytest.mark.parametrize("path", ["record", "launch"])
def test_a_helper_outliving_its_leader_is_unverified_on_both_paths(recovery, monkeypatch, caplog, path):
    """One answer for a group whose leader exits on SIGTERM while a helper lives on.

    Nothing proves a leaderless group ours, so the record-driven stop and the launch-time
    close_orphans both stop after SIGTERM and report it unverified; neither sends SIGKILL.
    """
    root, fake, _ = recovery
    directory = make_dir(root, "helper")
    browser_owner_lock.record_chrome(directory, SESSION, 102, 123.0, "/tmp/profile")
    fake[102] = FakeProcess(102, directory, chrome=True)
    leader = fake[102]
    sent = []

    def lookup(pid):
        if pid not in fake:
            raise browser_identity.BrowserPidAbsent
        return fake[pid]

    def send(pgid, selected):
        sent.append(selected)
        if selected == signal.SIGTERM:
            del fake[pgid]  # the leader exits; a helper that ignored SIGTERM keeps the group alive

    clock = iter(range(1000))
    monkeypatch.setattr(browser_identity, "process_for_identity", lookup)
    monkeypatch.setattr(processes, "stop_orphan_group", REAL_STOP)
    monkeypatch.setattr(processes, "_group_pids", lambda pgid, **_: (pgid,))
    monkeypatch.setattr(processes, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda _: None))
    monkeypatch.setattr(os, "killpg", send)
    if path == "record":
        browser_orphan.close_dead_sessions(root / "own", root=root)
        assert "browser orphan could not be verified pid=102" in caplog.text
    else:
        monkeypatch.setattr(browser_orphan.psutil, "process_iter", lambda *_: [leader])
        with pytest.raises(browser_orphan.BrowserProfileInUse, match="102"):
            browser_orphan.close_orphans("/tmp/profile", frozenset())
    assert sent == [signal.SIGTERM]


def test_a_stale_record_never_signals_the_process_now_holding_its_pid(tmp_path, monkeypatch, caplog):
    """A record outlives its Chrome. Its birth time keeps the sweep off whatever holds the pid now."""
    root = tmp_path / "sockets"
    root.mkdir(mode=0o700)
    directory = make_dir(root, "dead")
    monkeypatch.setattr(browser_ports, "SOCKET_ROOT", root)
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        birth = psutil.Process(unrelated.pid).create_time()
        browser_owner_lock.record_chrome(directory, SESSION, unrelated.pid, birth - 1.0, str(tmp_path))
        browser_orphan.close_dead_sessions(root / "own", root=root)
        assert unrelated.poll() is None
        assert f"browser orphan could not be verified pid={unrelated.pid}" in caplog.text
    finally:
        unrelated.kill()
        unrelated.wait(timeout=10)


DYING_BACKEND = """
import asyncio, sys
from pathlib import Path
from host_access import browser_chrome, browser_owner_lock, browser_ports
directory, profile, chrome, ports = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
browser_ports.RESERVED_PORTS = tuple(int(port) for port in ports.split(","))
browser_chrome.chrome_argv = lambda *_, **__: [chrome, "-c", "import time; time.sleep(120)",
                                               f"--user-data-dir={profile}", "--headless=new"]
def no_listener(*_):
    raise OSError("fixture: Chrome never listens, so the launch keeps waiting")
browser_chrome.require_listener = no_listener
browser_owner_lock.acquire(directory, create=True)
asyncio.run(browser_chrome.ChromeProcess.launch(chrome, profile=profile, headless=True, ua=None,
                                                record=(directory, "session123456")))
"""


def _wait_for(condition, what, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            pytest.fail(f"fixture: {what}")
        time.sleep(0.05)


def _free_ports():
    ports = []
    for _ in range(2):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        ports.append(str(probe.getsockname()[1]))
        probe.close()
    return ",".join(ports)


@pytest.mark.skipif(sys.platform != "darwin", reason="only the macOS launch records Chrome; Linux keeps native-owned launch")
def test_a_dead_backends_starting_chrome_is_stopped_by_the_next_sweep(tmp_path, monkeypatch):
    """The CPU leak: a backend dies while its headless Chrome starts, and no session file names it.

    A real backend process launches a stand-in Chrome through ChromeProcess.launch and is killed
    while the launch waits for the listener. The kernel frees its owner.lock and launchd adopts
    the Chrome. The next backend's sweep finds that Chrome only through the record the launch
    wrote at the spawn, and stops it.
    """
    root = tmp_path / "sockets"
    root.mkdir(mode=0o700)
    directory = root / "dead"
    directory.mkdir(mode=0o700)
    profile = tmp_path / "profile"
    profile.mkdir()
    chrome = tmp_path / "chrome"
    chrome.symlink_to(sys.executable)
    record = directory / f"{SESSION}.chrome"
    backend = subprocess.Popen(
        [sys.executable, "-c", DYING_BACKEND, str(directory), str(profile), str(chrome), _free_ports()],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
    leaked = None
    try:
        _wait_for(lambda: record.exists() or backend.poll() is not None, "the launch wrote no record")
        assert backend.poll() is None
        leaked = json.loads(record.read_text())["pid"]
        backend.kill()
        backend.wait(timeout=10)
        _wait_for(lambda: psutil.Process(leaked).ppid() == 1, "launchd did not adopt the Chrome")
        assert not list(directory.glob("*.pid"))  # no session file: only the record names this Chrome
        monkeypatch.setattr(browser_ports, "SOCKET_ROOT", root)
        browser_orphan.close_dead_sessions(root / "own", root=root)
        _wait_for(lambda: not psutil.pid_exists(leaked), "the sweep left the Chrome running", timeout=5)
    finally:
        if backend.poll() is None:
            backend.kill()
            backend.wait(timeout=10)
        if leaked is not None and psutil.pid_exists(leaked):
            os.killpg(leaked, signal.SIGKILL)
