"""Dead browser owners are recovered without signalling live or unowned sessions."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from types import SimpleNamespace

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
