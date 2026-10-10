"""Real launcher/native-child boundaries must not retain a provider writer."""

import asyncio
import os
import signal
import sys

import pytest

from subagents.codex_app_server import CodexAppServerSession

fcntl = pytest.importorskip("fcntl")

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")

CHILD = '''
import fcntl, os, signal, sys, time
from pathlib import Path
lock = open(sys.argv[1], "w")
fcntl.flock(lock, fcntl.LOCK_EX)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
# Publish atomically: waiters poll exists(), and write_text creates the file empty first.
Path(sys.argv[2] + ".tmp").write_text(str(os.getpid()))
os.replace(sys.argv[2] + ".tmp", sys.argv[2])
if sys.argv[3] == "closed":
    for fd in (0, 1, 2):
        os.close(fd)
while True:
    time.sleep(1)
'''
LAUNCHER = '''
import subprocess, sys, time
from pathlib import Path
subprocess.Popen([sys.executable, "-c", sys.argv[1], *sys.argv[2:5]])
while not Path(sys.argv[3]).exists():
    time.sleep(.01)
if sys.argv[5] == "exit":
    sys.exit(0)
while True:
    time.sleep(1)
'''


async def launch(tmp_path, monkeypatch, *, parent_exit=False, closed_stdio=False, on_exit=None):
    lock, ready = tmp_path / "writer.lock", tmp_path / "ready"
    session = CodexAppServerSession(binary=sys.executable, task=None, control_only=True, on_exit=on_exit)
    monkeypatch.setattr(session, "build_env", lambda: dict(os.environ))
    monkeypatch.setattr(session, "build_cmd", lambda: [
        sys.executable, "-c", LAUNCHER, CHILD, str(lock), str(ready),
        "closed" if closed_stdio else "open", "exit" if parent_exit else "stay",
    ])

    async def initialize():
        async with asyncio.timeout(3):
            while not ready.exists():
                await asyncio.sleep(.01)
            if parent_exit:
                while session._proc.returncode is None:
                    await asyncio.sleep(.01)

    monkeypatch.setattr(session, "_initialize", initialize)
    await session.start()
    return session, lock, int(ready.read_text())


def assert_writer_released(path):
    with path.open() as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


async def cleanup(session, child):
    # Fail-first cleanup is PID-bound to this test's known child, never a group.
    try:
        os.kill(child, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await session.terminate(timeout=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_exit,closed_stdio", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("method", ["close", "terminate"])
async def test_teardown_releases_native_writer(tmp_path, monkeypatch, parent_exit, closed_stdio, method):
    session, lock, child = await launch(
        tmp_path, monkeypatch, parent_exit=parent_exit, closed_stdio=closed_stdio,
    )
    def forbidden_enumeration():
        raise PermissionError("protected host does not allow enumeration")
    monkeypatch.setattr("psutil.process_iter", forbidden_enumeration)
    try:
        await getattr(session, method)(timeout=.2)
        assert_writer_released(lock)
        await session.terminate(timeout=.2)  # Idempotent after group disappears.
    finally:
        await cleanup(session, child)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["reader", "receipt"])
async def test_fatal_failure_releases_writer_and_preserves_sibling(tmp_path, monkeypatch, failure):
    from subagents.codex_app_server_reader import _handle_reader_crash
    import logging

    sibling_dir = tmp_path / "sibling"
    sibling_dir.mkdir()
    session, lock, child = await launch(tmp_path, monkeypatch)
    sibling, sibling_lock, sibling_child = await launch(sibling_dir, monkeypatch)
    exits = []

    async def on_exit(code, error):
        exits.append((code, error))

    session._on_exit = on_exit
    try:
        if failure == "reader":
            _handle_reader_crash(session, RuntimeError("fixture failure"), logging.getLogger(__name__))
        else:
            from subagents.codex_tool_requests import ToolRequests
            session._tool_requests = ToolRequests(session)
            # A missing current receipt cursor is a real fatal callback path.
            await session._tool_requests._execute({"params": {}})
        await asyncio.wait_for(asyncio.shield(session._reader), 1)
        assert_writer_released(lock)
        assert not session._closing
        assert len(exits) == 1
        with pytest.raises(BlockingIOError):
            assert_writer_released(sibling_lock)
        assert sibling._proc.returncode is None
    finally:
        await cleanup(session, child)
        await cleanup(sibling, sibling_child)


def test_reused_group_leader_is_never_signalled(monkeypatch):
    from subagents.codex_process import CodexProcessGroup

    owned = CodexProcessGroup(os.getpid())
    assert owned.created is not None
    owned.created -= 1
    signals = []
    monkeypatch.setattr(os, "killpg", lambda *args: signals.append(args))
    with pytest.raises(RuntimeError, match="identity changed"):
        owned.send(signal.SIGKILL)
    assert signals == []


@pytest.mark.asyncio
async def test_group_permission_failure_is_not_cleanup_success(monkeypatch):
    from subagents.codex_process import CodexProcessGroup

    owned = CodexProcessGroup(os.getpid())
    monkeypatch.setattr(owned, "_assert_binding", lambda: None)
    def denied(*_args):
        raise PermissionError("signal denied")
    monkeypatch.setattr(os, "killpg", denied)
    with pytest.raises(TimeoutError):
        await owned.wait(.1)
    assert not owned.released


def test_killed_group_is_not_signalled_again(monkeypatch):
    from subagents.codex_process import CodexProcessGroup

    owned = CodexProcessGroup(os.getpid())
    monkeypatch.setattr(owned, "_assert_binding", lambda: None)
    signals = []

    def darwin_killpg(pgid, selected_signal):
        # Darwin denies signals to a group whose members are all unreaped zombies.
        if signals:
            raise PermissionError("group already killed")
        signals.append((pgid, selected_signal))

    monkeypatch.setattr(os, "killpg", darwin_killpg)
    owned.send(signal.SIGKILL)
    owned.send(signal.SIGKILL)
    owned.send(signal.SIGTERM)
    assert signals == [(os.getpid(), signal.SIGKILL)]


@pytest.mark.asyncio
async def test_exact_stop_then_fence_terminate_succeeds(tmp_path, monkeypatch):
    # Hard-cap Stop kills first, then the fence terminates the same session.
    session, lock, child = await launch(tmp_path, monkeypatch)
    try:
        session._request_terminate()
        await session.terminate(timeout=1)
        assert_writer_released(lock)
    finally:
        await cleanup(session, child)


@pytest.mark.asyncio
async def test_natural_launcher_exit_still_releases_native_writer(tmp_path, monkeypatch):
    exits = []

    async def on_exit(code, error):
        exits.append((code, error))

    session, lock, child = await launch(
        tmp_path, monkeypatch, parent_exit=True, closed_stdio=True, on_exit=on_exit,
    )
    try:
        await asyncio.wait_for(asyncio.shield(session._reader), 1)
        assert exits == [(0, "")]
        assert_writer_released(lock)
        assert not session._closing
    finally:
        await cleanup(session, child)


@pytest.mark.asyncio
@pytest.mark.parametrize("hangs_after_eof", [False, True])
async def test_launcher_exit_after_stdout_eof_is_bounded(tmp_path, monkeypatch, hangs_after_eof):
    from subagents import codex_app_server_reader

    monkeypatch.setattr(codex_app_server_reader, "PROCESS_EXIT_TIMEOUT_SECONDS", .25, raising=False)
    exits = []

    async def on_exit(code, error):
        exits.append((code, error))

    session = CodexAppServerSession(
        binary=sys.executable, task=None, control_only=True, on_exit=on_exit,
    )
    monkeypatch.setattr(session, "build_env", lambda: dict(os.environ))
    monkeypatch.setattr(session, "build_cmd", lambda: [
        sys.executable, "-c", "import os, sys, time; os.close(1); time.sleep(float(sys.argv[1]))",
        "60" if hangs_after_eof else ".05",
    ])

    async def initialize():
        return None

    monkeypatch.setattr(session, "_initialize", initialize)
    await session.start()
    try:
        await asyncio.wait_for(asyncio.shield(session._reader), 1)
        assert len(exits) == 1
        if hangs_after_eof:
            assert exits[0][0] == -signal.SIGKILL
            assert "TimeoutError" in exits[0][1]
        else:
            assert exits == [(0, "")]
        assert session._process_group.released
        assert not session._closing
    finally:
        await session.terminate(timeout=1)
