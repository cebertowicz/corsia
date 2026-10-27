"""The OS tells the supervisor its oMLX server exited, and the supervisor acts on it (macOS only).

kqueue's process-exit event is delivered for a child and for an adopted pid alike. What the
supervisor does with an exit is in ``test_omlx_restart.py``, which runs everywhere.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm import local_keep_loaded  # noqa: E402
from llm.providers import omlx_restart as restart  # noqa: E402
from llm.providers import omlx_supervisor as sup  # noqa: E402

SID = "ns-exit-event-test"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from infra import config as cfg

    async def no_reload(*_args):
        return None

    monkeypatch.setattr(cfg, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(sup, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(restart, "emit_product_event", lambda *_a, **_k: None)
    monkeypatch.setattr(local_keep_loaded, "reload_after_restart", no_reload)
    yield
    sup.stop_watching()
    sup._life.clear()


@pytest.fixture
def starts(monkeypatch):
    """Replace the real server start with a recorder that reports success."""
    calls: list[dict] = []
    fired = asyncio.Event()

    async def fake_start(cfg):
        calls.append(cfg)
        fired.set()
        return {"ok": True, "pid": 4242}

    monkeypatch.setattr(sup, "start_server", fake_start)
    return calls, fired


def sleeper(seconds: str = "60") -> subprocess.Popen:
    return subprocess.Popen(
        ["sleep", seconds], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def not_our_child() -> int:
    """A live pid whose parent is a short-lived shell, like a server adopted after a backend restart."""
    out = subprocess.run(
        ["/bin/sh", "-c", "sleep 60 >/dev/null 2>&1 & echo $!"], capture_output=True, text=True, check=True,
    )
    return int(out.stdout.strip())


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0.02)


async def test_a_killed_server_is_restarted(starts):
    calls, fired = starts
    proc = sleeper()
    sup.watch(SID, proc.pid)
    proc.send_signal(signal.SIGKILL)
    await asyncio.wait_for(fired.wait(), timeout=5)
    assert calls == [{"server_id": SID}]
    proc.wait(timeout=2)


async def test_an_exit_of_an_adopted_pid_that_is_not_our_child_restarts_the_server(starts):
    calls, fired = starts
    pid = not_our_child()
    sup.watch(SID, pid)
    os.kill(pid, signal.SIGKILL)
    await asyncio.wait_for(fired.wait(), timeout=5)
    assert calls == [{"server_id": SID}]


async def test_a_pid_already_gone_when_watched_counts_as_an_exit(starts):
    calls, fired = starts
    proc = sleeper()
    proc.kill()
    proc.wait(timeout=2)
    sup.watch(SID, proc.pid)
    await asyncio.wait_for(fired.wait(), timeout=5)
    assert calls == [{"server_id": SID}]


async def test_a_clean_exit_zero_is_not_restarted(starts):
    calls, _ = starts
    proc = sleeper("0.3")  # sleep ends by itself with status 0
    sup.watch(SID, proc.pid)
    await asyncio.sleep(0.8)
    proc.wait(timeout=2)
    assert calls == []


async def test_an_exit_after_an_announced_stop_is_not_a_crash(starts):
    calls, _ = starts
    proc = sleeper()
    sup.watch(SID, proc.pid)
    sup._life[SID].announce_stop()
    proc.kill()
    await settle()
    assert calls == []
    proc.wait(timeout=2)


async def test_stop_server_drops_the_watch_so_its_exit_is_never_a_crash(starts):
    calls, _ = starts
    proc = sleeper()
    sup._write_pid(SID, proc.pid)
    sup.watch(SID, proc.pid)
    result = await sup.stop_server({"server_id": SID})
    await settle()
    assert result["ok"] is True and calls == [] and SID not in sup._life
    proc.wait(timeout=5)


async def test_stop_all_servers_never_restarts_any_server(starts):
    calls, _ = starts
    procs = {"ns-a": sleeper(), "ns-b": sleeper()}
    for sid, proc in procs.items():
        sup._write_pid(sid, proc.pid)
        sup.watch(sid, proc.pid)
    results = await sup.stop_all_servers()
    await settle()
    assert [r["ok"] for r in results] == [True, True]
    assert calls == []
    for proc in procs.values():
        proc.wait(timeout=5)


async def test_backend_shutdown_never_restarts_a_server(starts):
    calls, _ = starts
    proc = sleeper()
    sup.watch(SID, proc.pid)
    sup.stop_watching()
    proc.kill()
    await settle()
    assert calls == []
    proc.wait(timeout=2)


async def test_a_server_that_exits_while_a_restart_is_running_gets_a_new_restart(monkeypatch):
    calls: list[dict] = []
    second = asyncio.Event()

    async def start_then_lose_the_server(cfg):
        calls.append(cfg)
        if len(calls) == 1:  # the server the restart brought up dies before the restart has finished
            doomed = sleeper()
            sup.watch(SID, doomed.pid)
            doomed.kill()
            await asyncio.sleep(5)
        else:
            second.set()
        return {"ok": True, "pid": 4242}

    monkeypatch.setattr(sup, "start_server", start_then_lose_the_server)
    proc = sleeper()
    sup.watch(SID, proc.pid)
    proc.kill()
    await asyncio.wait_for(second.wait(), timeout=5)
    assert len(calls) == 2
