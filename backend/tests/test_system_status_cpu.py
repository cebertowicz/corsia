"""GET /api/system's CPU figure: psutil snapshots, in-process, thread-safe.

Every open page polls this route, and the reading runs on whichever default-executor
thread asyncio.to_thread picks. psutil.cpu_percent(interval=None) keys its baseline by
thread id, so that figure read 0.0 on each new worker thread. These tests pin the
replacement: the figure is the busy delta between two cpu_times() snapshots, it needs
no subprocess, and it holds when consecutive polls land on different threads.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections import namedtuple

import pytest
from auth_fixtures import OWNER_TOKEN
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Field set covering both platforms: macOS reports no iowait, Linux reports no guest
# time on every host, so the helper must read each field by name.
Scpu = namedtuple("Scpu", "user nice system idle iowait")
SNAPSHOT_1 = Scpu(user=100.0, nice=0.0, system=50.0, idle=800.0, iowait=50.0)
SNAPSHOT_2 = Scpu(user=150.0, nice=0.0, system=100.0, idle=850.0, iowait=50.0)
# Nothing but idle time elapses after SNAPSHOT_2, so a window that opens on it reads 0.
SNAPSHOT_3 = Scpu(user=150.0, nice=0.0, system=100.0, idle=1050.0, iowait=50.0)
# 150 CPU seconds elapse, 50 of them idle -> 66.7% busy, 33.3% idle.
EXPECTED = {"usage_pct": 66.7, "idle_pct": 33.3}


def _cold_window(monkeypatch) -> None:
    """Forget any previous reading, so a test really starts from nothing."""
    from infra import host_cpu

    monkeypatch.setattr(host_cpu, "_prev_times", None)
    monkeypatch.setattr(host_cpu, "_prev_monotonic", 0.0)
    monkeypatch.setattr(host_cpu, "_prev_pct", 0.0)


@pytest.fixture
def system_client(isolated_db, monkeypatch):
    """The system router with a cold status cache and a cold CPU window."""
    from api import system

    monkeypatch.setitem(system._system_cache, "data", None)
    _cold_window(monkeypatch)
    app = FastAPI()
    app.include_router(system.router)
    with TestClient(app) as client:
        yield client


def _system_cpu(client) -> dict:
    body = client.get(
        "/api/system", headers={"Authorization": f"Bearer {OWNER_TOKEN}"}
    ).json()
    return body["cpu"]


def _burn(stop: threading.Event) -> None:
    """Hold a core, so a reading taken while it runs cannot honestly read 0%."""
    while not stop.is_set():
        pass


def _in_fresh_thread(read) -> float:
    """Run a reading on a new thread, the way asyncio.to_thread picks workers."""
    box: list[float] = []
    thread = threading.Thread(target=lambda: box.append(read()))
    thread.start()
    thread.join(timeout=15)
    assert box, "the reading did not finish"
    return box[0]


def test_cpu_is_the_busy_delta_between_two_cpu_times_snapshots(
    system_client, monkeypatch
):
    import psutil
    from infra import host_cpu

    monkeypatch.setattr(host_cpu, "_prev_times", SNAPSHOT_1)
    monkeypatch.setattr(psutil, "cpu_times", lambda: SNAPSHOT_2)

    assert _system_cpu(system_client) == EXPECTED


def test_cpu_needs_no_subprocess(system_client, monkeypatch):
    import psutil
    from infra import host_cpu

    started: list[tuple] = []

    def record_run(*args, **kwargs):
        started.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", record_run)
    monkeypatch.setattr(host_cpu, "_prev_times", SNAPSHOT_1)
    monkeypatch.setattr(psutil, "cpu_times", lambda: SNAPSHOT_2)

    cpu = _system_cpu(system_client)

    assert started == []
    assert cpu == EXPECTED


def test_first_reading_measures_its_own_window(monkeypatch):
    """No previous snapshot: it samples a window instead of returning 0.0."""
    import psutil
    from infra import host_cpu

    _cold_window(monkeypatch)
    snapshots = iter([SNAPSHOT_1, SNAPSHOT_2])
    monkeypatch.setattr(psutil, "cpu_times", lambda: next(snapshots))

    started = time.monotonic()
    usage = host_cpu.usage_pct()

    assert usage == pytest.approx(200 / 3, abs=0.01)  # 66.666...% busy
    assert time.monotonic() - started >= host_cpu.FIRST_WINDOW_SECONDS * 0.9


def test_rapid_polls_share_one_window(monkeypatch):
    """Pages poll unsynchronized; a milliseconds-old window is tick noise."""
    import psutil
    from infra import host_cpu

    class Clock:
        """A clock that only moves when the code under test sleeps or is advanced."""

        def __init__(self) -> None:
            self.now = 1000.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    clock = Clock()
    snapshots = [SNAPSHOT_1, SNAPSHOT_2, SNAPSHOT_3]
    taken: list[Scpu] = []

    def cpu_times() -> Scpu:
        snapshot = snapshots[len(taken)]
        taken.append(snapshot)
        return snapshot

    _cold_window(monkeypatch)
    monkeypatch.setattr(host_cpu, "time", clock)
    monkeypatch.setattr(psutil, "cpu_times", cpu_times)

    first = host_cpu.usage_pct()
    clock.now += 0.01
    second = host_cpu.usage_pct()

    assert first == pytest.approx(200 / 3, abs=0.01)
    assert second == first
    assert taken == [SNAPSHOT_1, SNAPSHOT_2]  # no snapshot taken for the short window
    assert host_cpu._prev_times is SNAPSHOT_2  # and the window stayed open

    clock.now += host_cpu.MIN_WINDOW_SECONDS
    assert host_cpu.usage_pct() == 0.0  # the reopened window reads on its own
    assert taken == [SNAPSHOT_1, SNAPSHOT_2, SNAPSHOT_3]


def test_reading_holds_across_worker_threads(monkeypatch):
    """The regression: a poll on a new thread used to read 0% busy."""
    from infra import host_cpu

    _cold_window(monkeypatch)
    stop = threading.Event()
    burners = [
        threading.Thread(target=_burn, args=(stop,), daemon=True) for _ in range(4)
    ]
    for burner in burners:
        burner.start()
    try:
        _in_fresh_thread(host_cpu.usage_pct)
        time.sleep(host_cpu.MIN_WINDOW_SECONDS + 0.2)  # long enough to reopen
        second = _in_fresh_thread(host_cpu.usage_pct)
    finally:
        stop.set()
        for burner in burners:
            burner.join(timeout=5)

    assert second > 1.0, f"a busy host read {second}% on a fresh worker thread"


def test_cpu_fields_render_from_real_psutil(system_client):
    """Uncollected by the fakes above: the route really reads host CPU."""
    cpu = _system_cpu(system_client)

    assert "error" not in cpu
    assert 0.0 <= cpu["usage_pct"] <= 100.0
    assert 0.0 <= cpu["idle_pct"] <= 100.0


def test_cpu_error_branch_survives(system_client, monkeypatch):
    import psutil

    def unreadable():
        raise RuntimeError("cpu unavailable")

    monkeypatch.setattr(psutil, "cpu_times", unreadable)

    assert _system_cpu(system_client) == {"error": "cpu unavailable"}
