"""CPU attribution through real DB executor jobs."""

import asyncio
import functools
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from infra import cpu_sampler, db_async
from infra.db_connection import mark_thread_lane


def short_cpu_job():
    end = time.thread_time() + 0.02
    while time.thread_time() < end:
        pass


class Work:
    def method(self):
        short_cpu_job()


@pytest.mark.asyncio
async def test_short_jobs_named_by_lane_and_unwrapped(monkeypatch, caplog):
    sampler = cpu_sampler.CpuSampler()
    monkeypatch.setattr(cpu_sampler, "_sampler", sampler)
    caplog.set_level(logging.WARNING, logger="infra.cpu_sampler")
    await db_async.run_in_db(short_cpu_job)
    await db_async.run_read(functools.partial(Work().method))
    await db_async.run_bulk(short_cpu_job)
    await db_async.run_off_db(short_cpu_job)
    sampler.emit_summary()
    record = next(r.message for r in caplog.records if r.message.startswith("cpu sampler jobs:"))
    assert "db:" in record and "db-read:" in record and "db-bulk:" in record
    assert "short_cpu_job" in record and "Work.method" in record
    assert "idle" not in record


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["read", "bulk"])
async def test_renamed_thread_keeps_its_lane(monkeypatch, lane):
    sampler = cpu_sampler.CpuSampler()
    monkeypatch.setattr(cpu_sampler, "_sampler", sampler)

    def rename():
        threading.current_thread().name = "renamed-worker"

    with ThreadPoolExecutor(max_workers=1, initializer=mark_thread_lane, initargs=(lane,)) as pool:
        monkeypatch.setattr(db_async, f"_{lane}_pool", pool)
        run = db_async.run_read if lane == "read" else db_async.run_bulk
        await run(rename)
        await run(short_cpu_job)
    assert sum(count for (job_lane, _), (count, _, _) in sampler._job_cpu.items() if job_lane == f"db-{lane}") == 2


@pytest.mark.asyncio
async def test_sampler_off_does_not_accumulate(monkeypatch):
    sampler = cpu_sampler.CpuSampler()
    monkeypatch.setattr(cpu_sampler, "_sampler", None)
    def unexpected(*args):
        pytest.fail("disabled sampler ran a measurement")
    monkeypatch.setattr(cpu_sampler, "run_profiled_db_job", unexpected)
    await db_async.run_in_db(lambda: None)
    assert sampler._job_cpu == {}


@pytest.mark.asyncio
async def test_queued_job_uses_sampler_state_at_execution(monkeypatch):
    sampler = cpu_sampler.CpuSampler()
    monkeypatch.setattr(cpu_sampler, "_sampler", None)
    started, release = threading.Event(), threading.Event()
    def block():
        started.set()
        release.wait()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="db") as pool:
        monkeypatch.setattr(db_async, "_pool", pool)
        first = asyncio.create_task(db_async.run_in_db(block))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            second = asyncio.create_task(db_async.run_in_db(short_cpu_job))
            await asyncio.sleep(0)
            monkeypatch.setattr(cpu_sampler, "_sampler", sampler)
        finally:
            release.set()
        await asyncio.gather(first, second)
    assert sum(item[0] for item in sampler._job_cpu.values()) == 1
    assert any("short_cpu_job" in name for _, name in sampler._job_cpu)


@pytest.mark.asyncio
async def test_job_failure_unchanged_and_timing_preserved(monkeypatch):
    sampler = cpu_sampler.CpuSampler()
    monkeypatch.setattr(cpu_sampler, "_sampler", sampler)
    failure = ValueError("job failure")
    def fail():
        raise failure
    with pytest.raises(ValueError) as caught:
        await db_async.run_in_db(fail)
    assert caught.value is failure
    assert len(failure._corsia_db_timing) == 2
    assert sum(item[0] for item in sampler._job_cpu.values()) == 1


@pytest.mark.asyncio
async def test_diagnostic_failure_does_not_change_job_result(monkeypatch):
    sampler = cpu_sampler.CpuSampler()
    monkeypatch.setattr(cpu_sampler, "_sampler", sampler)
    def broken_record(*args):
        raise RuntimeError("measurement failed")
    monkeypatch.setattr(sampler, "record_job_cpu", broken_record)
    assert await db_async.run_in_db(lambda: 42) == 42
    assert sampler._job_errors == 1


@pytest.mark.asyncio
async def test_job_and_diagnostic_failure_preserve_original_exception(monkeypatch):
    sampler = cpu_sampler.CpuSampler()
    monkeypatch.setattr(cpu_sampler, "_sampler", sampler)
    def broken_record(*args):
        raise RuntimeError("measurement failed")
    monkeypatch.setattr(sampler, "record_job_cpu", broken_record)
    failure = ValueError("job failure")
    def fail():
        raise failure
    with pytest.raises(ValueError) as caught:
        await db_async.run_in_db(fail)
    assert caught.value is failure
    assert len(failure._corsia_db_timing) == 2
    assert sampler._job_errors == 1


def test_summary_is_bounded_with_many_jobs(caplog):
    sampler = cpu_sampler.CpuSampler()
    caplog.set_level(logging.WARNING, logger="infra.cpu_sampler")
    for n in range(50):
        sampler.record_job_cpu("db", f"module.callable_{n}", 1000000 + n)
    sampler.emit_summary()
    record = next(r.message for r in caplog.records if r.message.startswith("cpu sampler jobs:"))
    assert len(record) <= 1400
