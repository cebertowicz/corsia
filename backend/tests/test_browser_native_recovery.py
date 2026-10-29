"""Native proof that recovery stops dead owners without touching a live owner."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import tempfile
from pathlib import Path

import psutil
import pytest

from host_access import browser_orphan

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get("CORSIA_NATIVE_BROWSER_TEST") != "1", reason="explicit native-browser job only",
)]

_CHILD = r'''
import asyncio, json, os, sys
from pathlib import Path
from host_access import browser_ports
from host_access.browser_sessions import BrowserSessionOwner

browser_ports.SOCKET_ROOT = Path(sys.argv[1])
os.environ.pop("CI", None)
for key in tuple(os.environ):
    if key.startswith("AGENT_BROWSER_"):
        os.environ.pop(key)
config = Path(sys.argv[1]) / f"config-{os.getpid()}.json"
config.write_text("{}")
os.environ["AGENT_BROWSER_CONFIG"] = str(config)
os.environ["AGENT_BROWSER_PROFILE"] = str(Path(sys.argv[1]) / f"profile-{os.getpid()}")
os.environ["AGENT_BROWSER_EXECUTABLE_PATH"] = os.environ["CORSIA_NATIVE_CHROME_BINARY"]

async def main():
    owner = BrowserSessionOwner(namespace="corsia-test", binary=os.environ["CORSIA_NATIVE_BROWSER_BINARY"])
    result = await owner.run("native-recovery", ["open", "about:blank"], timeout=90)
    if result.startswith("Error:"):
        raise RuntimeError("native browser could not start")
    session = owner._sessions["native-recovery"]
    print(json.dumps({"pid": session.record.pid, "directory": str(owner.socket_dir)}), flush=True)
    command = await asyncio.to_thread(sys.stdin.readline)
    if command.strip() == "die":
        os._exit(0)
    await owner.shutdown()

asyncio.run(main())
'''


async def _start_backend(root: Path):
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-u", "-c", _CHILD, str(root), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert child.stdout is not None and child.stderr is not None
        line = await asyncio.wait_for(child.stdout.readline(), 120)
        if not line:
            stderr = await child.stderr.read()
            raise AssertionError(f"native test backend exited before startup: {stderr.decode(errors='replace')[-1000:]}")
        return child, json.loads(line)
    except BaseException:
        if child.returncode is None:
            child.kill()
            await child.wait()
        raise


def _snapshot(pid: int) -> dict[int, float]:
    daemon = psutil.Process(pid)
    descendants = daemon.children(recursive=True)
    assert any("chrome" in process.name().lower() for process in descendants), "no native Chrome descendant"
    return {process.pid: process.create_time() for process in [daemon, *descendants]}


def _still_live(pid: int, birth: float) -> bool:
    try:
        process = psutil.Process(pid)
        return (process.create_time() == birth and process.is_running()
                and process.status() != psutil.STATUS_ZOMBIE)
    except psutil.NoSuchProcess:
        return False


def _live_chrome(pid: int, birth: float) -> bool:
    try:
        return _still_live(pid, birth) and "chrome" in psutil.Process(pid).name().lower()
    except psutil.NoSuchProcess:
        return False


def _stop_test_processes(snapshot: dict[int, float]) -> None:
    # Only exact processes captured from this test's own native daemons may be signalled.
    for pid, birth in snapshot.items():
        if _still_live(pid, birth):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


async def _command(child, command: str) -> None:
    child.stdin.write((command + "\n").encode())
    await child.stdin.drain()
    await asyncio.wait_for(child.wait(), 30)


async def test_native_dead_owner_sweep_keeps_a_live_owner(tmp_path):
    """A dead native daemon and its Chrome exit, while the live lock protects its session."""
    # Unix socket paths must fit the platform limit; the test owns this short temporary root.
    with tempfile.TemporaryDirectory(prefix="corsia-native-", dir="/tmp") as short:
        root = Path(short)
        (tmp_path / "sockets").symlink_to(root, target_is_directory=True)
        dead = live = None
        captured: dict[int, float] = {}
        try:
            dead, dead_info = await _start_backend(root)
            live, live_info = await _start_backend(root)
            dead_pids = _snapshot(dead_info["pid"])
            live_pids = _snapshot(live_info["pid"])
            captured.update(dead_pids)
            captured.update(live_pids)
            await _command(dead, "die")
            await asyncio.to_thread(browser_orphan.close_dead_sessions, root / "own", root=root)
            for _ in range(100):
                if not any(_still_live(pid, birth) for pid, birth in dead_pids.items()):
                    break
                await asyncio.sleep(0.1)
            assert not any(_still_live(pid, birth) for pid, birth in dead_pids.items())
            assert _still_live(live_info["pid"], live_pids[live_info["pid"]])
            assert any(pid != live_info["pid"] and _live_chrome(pid, birth)
                       for pid, birth in live_pids.items())
        finally:
            if live is not None and live.returncode is None:
                try:
                    await _command(live, "close")
                except (OSError, asyncio.TimeoutError):
                    live.kill()
                    await live.wait()
            if dead is not None and dead.returncode is None:
                try:
                    await _command(dead, "close")
                except (OSError, asyncio.TimeoutError):
                    dead.kill()
                    await dead.wait()
            _stop_test_processes(captured)
