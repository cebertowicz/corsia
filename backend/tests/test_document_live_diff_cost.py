"""Distributed prose edits must not monopolize the process event loop."""
import asyncio
import gc
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from doceditor.live_document import file_hash
from doceditor.live_lane import Operation
from doceditor.live_text import apply_text, create
from tests.test_document_live_lane import open_lane


def prose_rewrite():
    vocabulary = ("The writer keeps each document readable while another person edits "
                  "the same passage and checks the examples before saving the final text").split()
    before, after = [], []
    word_index = 0
    for number in range(7200):
        words = vocabulary + [str(number)]
        changed = list(words)
        for index in range(len(words)):
            if word_index % 100 == 50:
                changed[index] = "revised"
            word_index += 1
        before.append(" ".join(words) + "\n")
        after.append(" ".join(changed) + "\n")
    return "".join(before)[:1_000_000], "".join(after)[:1_000_000]


def diff_probe():
    old, new = prose_rewrite()
    assert len(old.encode()) >= 1_000_000
    doc = create(old)
    started = time.perf_counter()
    apply_text(doc, new)
    elapsed = time.perf_counter() - started
    assert str(doc["text"]) == new
    print(f"distributed-prose-apply-text={elapsed:.6f}s;bytes={len(old.encode())}")
    return elapsed


class CallbackCost:
    """The longest event-loop callback's CPU time on the loop thread, GC included.

    Wall time under host contention measures the scheduler; this measures the
    synchronous step our code hands the loop.
    """

    def __init__(self):
        self.armed = False
        self.longest = (0.0, 0.0)
        self._thread = threading.get_ident()
        self._gc = 0.0
        self._gc_started = 0.0
        self._run = asyncio.events.Handle._run

    def _on_gc(self, phase, info):
        if threading.get_ident() != self._thread:
            return
        if phase == "start":
            self._gc_started = time.thread_time()
        else:
            self._gc += time.thread_time() - self._gc_started

    def __enter__(self):
        real_run, cost = self._run, self
        def run(handle):
            if not cost.armed:
                return real_run(handle)
            cost._gc, started = 0.0, time.thread_time()
            try:
                return real_run(handle)
            finally:
                cost.longest = max(cost.longest, (time.thread_time() - started, cost._gc))
        asyncio.events.Handle._run = run
        gc.callbacks.append(self._on_gc)
        return self

    def __exit__(self, *exc):
        asyncio.events.Handle._run = self._run
        gc.callbacks.remove(self._on_gc)


async def loop_probe(tmp_path):
    old, new = prose_rewrite()
    (tmp_path / "document.md").write_text(old)
    live, lane = await open_lane(tmp_path)
    imported = asyncio.Event()
    real_import = lane._import
    async def ready():
        result = await real_import()
        imported.set()
        await asyncio.sleep(0)
        return result
    lane._import = ready
    call = asyncio.create_task(lane.submit(Operation("write", content=new, base_hash=file_hash(old.encode()))))
    try:
        with CallbackCost() as cost:
            await imported.wait()
            cost.armed = True
            responses = []
            while not call.done():
                started = time.perf_counter()
                await asyncio.sleep(.001)
                responses.append(time.perf_counter() - started)
            cost.armed = False
        elapsed = max(responses)
        result = await call
        assert result.server_saved
        assert live.text == live.path.read_text() == new
        cpu, collected = cost.longest
        print(f"distributed-prose-loop-response-worst={elapsed:.6f}s;samples={len(responses)}")
        print(f"distributed-prose-longest-callback-cpu={cpu:.6f}s;gc={collected:.6f}s")
        return cpu
    finally:
        await asyncio.gather(call, return_exceptions=True)
        await lane.close()
        live.close()


async def least_loop_probe(tmp_path, runs=3):
    # Host contention only adds CPU time to the same work, so the least of a
    # few fresh lanes measures the code; a regression raises every run.
    costs = []
    for run in range(runs):
        (tmp_path / str(run)).mkdir()
        costs.append(await loop_probe(tmp_path / str(run)))
    return min(costs)


def run_probe(code, tmp_path):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    try:
        # The asserted measurements bound the cost; this only stops a hang
        # (cold import plus setup exceeds 5 s on a loaded host).
        result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                                capture_output=True, text=True, timeout=120, check=True)
    except subprocess.TimeoutExpired:
        pytest.fail("Distributed 1MB diff stalled the probe process for over 120 seconds")
    print(result.stdout.strip())
    return float(result.stdout.strip().splitlines()[-1])


def test_megabyte_distributed_prose_diff_finishes_under_one_second(tmp_path):
    elapsed = run_probe("from tests.test_document_live_diff_cost import diff_probe; print(diff_probe())", tmp_path)
    assert elapsed < 1.0


def test_megabyte_lane_write_holds_the_loop_under_100ms(tmp_path):
    cpu = run_probe("import asyncio; from pathlib import Path; "
                    "from tests.test_document_live_diff_cost import least_loop_probe; "
                    "print(asyncio.run(least_loop_probe(Path.cwd())))", tmp_path)
    assert cpu < .1
