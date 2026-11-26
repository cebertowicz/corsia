"""Startup sweep deletes unowned temporary directories without racing launches."""
import asyncio
import json
import multiprocessing
import os
import shutil
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from host_access import browser_chrome, browser_copy_sweep, browser_owner_lock


def test_sweep_preserves_live_saved_and_symlink(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    root.mkdir()
    sockets = tmp_path / "sockets"
    owner = sockets / "owner"
    owner.mkdir(parents=True)
    stale = root / "session-stale"
    live = root / "session-live"
    saved = root / "person"
    for folder in (stale, live, saved):
        folder.mkdir()
    link = root / "session-link"
    link.symlink_to(saved, target_is_directory=True)
    record = owner / "example.chrome"
    record.write_text(json.dumps({"pid": 54321, "profile": str(live)}))
    monkeypatch.setattr(browser_copy_sweep.os, "killpg", lambda *_args: None)
    assert browser_copy_sweep.sweep(profile_root=root, socket_root=sockets) == 1
    assert not stale.exists()
    assert live.is_dir() and saved.is_dir() and link.is_symlink()


def _hold_launch_lock(root, connection):
    fd = browser_copy_sweep.lock_profile_root(root, exclusive=False)
    try:
        connection.send(True)
        if connection.poll(10):
            connection.recv()
    finally:
        os.close(fd)
        connection.close()


def test_launch_lock_prevents_sweep_until_record_can_be_written(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    candidate = root / "session-starting"
    candidate.mkdir()
    fd = browser_copy_sweep.lock_profile_root(root, exclusive=False)
    try:
        assert browser_copy_sweep.sweep(profile_root=root, socket_root=tmp_path / "sockets") == 0
        assert candidate.is_dir()
    finally:
        os.close(fd)
    assert browser_copy_sweep.sweep(profile_root=root, socket_root=tmp_path / "sockets") == 1
    assert not candidate.exists()


def test_lock_coordinates_separate_processes(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    candidate = root / "session-starting"
    candidate.mkdir()
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_hold_launch_lock, args=(root, child))
    process.start()
    child.close()
    try:
        assert parent.poll(10) and parent.recv()
        assert browser_copy_sweep.sweep(profile_root=root, socket_root=tmp_path / "sockets") == 0
        assert candidate.is_dir()
    finally:
        parent.send(True)
        parent.close()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join()
    assert process.exitcode == 0
    assert browser_copy_sweep.sweep(profile_root=root, socket_root=tmp_path / "sockets") == 1


@pytest.mark.asyncio
async def test_launch_holds_shared_lock_from_creation_through_record(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    root.mkdir()
    sockets = tmp_path / "sockets"
    events = []
    real_mkdtemp = browser_chrome.tempfile.mkdtemp

    def make_profile(*args, **kwargs):
        result = real_mkdtemp(*args, **kwargs)
        assert browser_copy_sweep.sweep(profile_root=root, socket_root=sockets) == 0
        assert os.path.isdir(result)
        events.append("created")
        return result

    def record(_directory, _session, chrome, _profile):
        assert browser_copy_sweep.sweep(profile_root=root, socket_root=sockets) == 0
        assert chrome.temporary_profile.is_dir()
        owner = sockets / "owner"
        owner.mkdir(parents=True)
        browser_owner_lock.record_chrome(owner, "session123456", 123, 1.0, str(chrome.temporary_profile))
        events.append("recorded")

    class Port:
        def getsockname(self):
            return ("127.0.0.1", 43210)

        def close(self):
            pass

    monkeypatch.setattr(browser_chrome.browser_launch, "profile_root", lambda: root)
    monkeypatch.setattr(browser_chrome.tempfile, "mkdtemp", make_profile)
    monkeypatch.setattr(browser_chrome.browser_ports, "reserve_pair", lambda: (Port(), Port()))
    monkeypatch.setattr(browser_chrome.asyncio, "create_subprocess_exec",
                        AsyncMock(return_value=SimpleNamespace(pid=123, returncode=None)))
    monkeypatch.setattr(browser_owner_lock, "record_launched_chrome", record)
    monkeypatch.setattr(browser_chrome, "require_listener", lambda *_args: None)
    monkeypatch.setattr(browser_chrome, "browser_endpoint", lambda _port: "ws://test")
    chrome = await browser_chrome.ChromeProcess.launch("chrome", profile=None, headless=True, ua=None,
                                                       record=(sockets, "session"))
    assert events == ["created", "recorded"]
    monkeypatch.setattr(browser_copy_sweep.os, "killpg", lambda *_args: None)
    assert browser_copy_sweep.sweep(profile_root=root, socket_root=sockets) == 0
    assert chrome.temporary_profile.is_dir()
    def gone(*_args):
        raise ProcessLookupError
    monkeypatch.setattr(browser_copy_sweep.os, "killpg", gone)
    assert browser_copy_sweep.sweep(profile_root=root, socket_root=sockets) == 1
    assert not chrome.temporary_profile.exists()


@pytest.mark.asyncio
async def test_cancellation_waits_for_record_before_releasing_lock(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    root.mkdir()
    started, release = threading.Event(), threading.Event()

    class Port:
        def getsockname(self):
            return ("127.0.0.1", 43210)

        def close(self):
            pass

    def record(*_args):
        started.set()
        assert release.wait(10)

    async def close(chrome):
        shutil.rmtree(chrome.temporary_profile)

    monkeypatch.setattr(browser_chrome.browser_launch, "profile_root", lambda: root)
    monkeypatch.setattr(browser_chrome.browser_ports, "reserve_pair", lambda: (Port(), Port()))
    monkeypatch.setattr(browser_chrome.asyncio, "create_subprocess_exec",
                        AsyncMock(return_value=SimpleNamespace(pid=123, returncode=None)))
    monkeypatch.setattr(browser_owner_lock, "record_launched_chrome", record)
    monkeypatch.setattr(browser_chrome.ChromeProcess, "close", close)
    task = asyncio.create_task(browser_chrome.ChromeProcess.launch(
        "chrome", profile=None, headless=True, ua=None, record=(tmp_path, "session")))
    try:
        assert await asyncio.to_thread(started.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert browser_copy_sweep.sweep(profile_root=root, socket_root=tmp_path / "sockets") == 0
        assert list(root.glob("session-*"))
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(root.glob("session-*"))
    assert browser_copy_sweep.sweep(profile_root=root, socket_root=tmp_path / "sockets") == 0


def test_symlink_profile_root_is_not_followed(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    stale = actual / "session-stale"
    stale.mkdir()
    link = tmp_path / "profile-link"
    link.symlink_to(actual, target_is_directory=True)
    assert browser_copy_sweep.sweep(profile_root=link, socket_root=tmp_path / "sockets") == 0
    assert stale.is_dir()


def test_unreadable_record_holds_sweep(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    stale = root / "session-stale"
    stale.mkdir()
    owner = tmp_path / "sockets" / "owner"
    owner.mkdir(parents=True)
    (owner / "bad.chrome").write_text("not-json")
    assert browser_copy_sweep.sweep(profile_root=root, socket_root=owner.parent) == 0
    assert stale.is_dir()
