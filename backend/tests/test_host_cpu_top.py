from __future__ import annotations

import json
from types import SimpleNamespace

import psutil
import pytest

from tools import host_cpu_top

_RESULT_KEYS = {
    "processes", "unreadable_processes", "load_averages", "core_count", "sample_seconds",
}
_ROW_KEYS = {"pid", "name", "cpu_percent", "memory_rss_bytes", "start_time"}


class FakeProcess:
    def __init__(self, pid, failure=None):
        self.pid = pid
        self.failure = failure
        self.calls = 0

    def cpu_percent(self, interval=None):
        self.calls += 1
        if self.failure == self.calls:
            raise psutil.AccessDenied(self.pid) if self.calls == 1 else psutil.NoSuchProcess(self.pid)
        return float(self.pid)

    def name(self):
        return "worker"

    def memory_info(self):
        return SimpleNamespace(rss=1024)

    def create_time(self):
        return 123.0


@pytest.mark.asyncio
async def test_fields_bounds_and_unreadable(monkeypatch):
    processes = [FakeProcess(i) for i in range(1, 61)]
    processes += [FakeProcess(61, failure=1), FakeProcess(62, failure=2)]
    monkeypatch.setattr(host_cpu_top.psutil, "process_iter", lambda: iter(processes))
    monkeypatch.setattr(host_cpu_top.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(host_cpu_top.psutil, "getloadavg", lambda: (1.0, 2.0, 3.0))
    monkeypatch.setattr(host_cpu_top.psutil, "cpu_count", lambda: 8)

    result = json.loads(await host_cpu_top.HostCpuTopTool().execute(limit=100))
    assert set(result) == _RESULT_KEYS
    assert len(result["processes"]) == 50
    assert result["processes"][0]["pid"] == 60
    assert set(result["processes"][0]) == _ROW_KEYS
    assert result["unreadable_processes"] == 2
    assert result["sample_seconds"] == 1.0
    assert result["load_averages"] == [1.0, 2.0, 3.0]
    assert result["core_count"] == 8


@pytest.mark.asyncio
async def test_default_limit(monkeypatch):
    monkeypatch.setattr(host_cpu_top.psutil, "process_iter", lambda: iter(FakeProcess(i) for i in range(30)))
    monkeypatch.setattr(host_cpu_top.time, "sleep", lambda seconds: None)
    result = json.loads(await host_cpu_top.HostCpuTopTool().execute())
    assert len(result["processes"]) == 20


def test_real_host_sample_has_only_allowlisted_keys():
    result = host_cpu_top._sample(5)
    assert set(result) == _RESULT_KEYS
    assert all(set(row) == _ROW_KEYS for row in result["processes"])
    assert len(result["processes"]) <= 5
