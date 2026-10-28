"""Bound the repeated-text case that otherwise prevents native Stop joining."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


_SAMPLE = """
import time
from doceditor.live_text import create, apply_text
from doceditor.document_scope import MAX_TEXT_BYTES
text = 'x' * (MAX_TEXT_BYTES - 1)
doc = create(text)
start = time.perf_counter()
apply_text(doc, text + '!')
assert str(doc['text']) == text + '!'
print(time.perf_counter() - start)
"""


def test_repeated_maximum_text_diff_finishes_inside_stop_budget(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    try:
        result = subprocess.run(
            [sys.executable, "-c", _SAMPLE], cwd=tmp_path, env=env,
            capture_output=True, text=True, timeout=5, check=True,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("MAX_TEXT_BYTES repeated-text diff exceeded 5 seconds of the 10 second Stop join")
    assert float(result.stdout.strip()) <= 1.0


def maximum_message(live):
    from pycrdt import Doc, Text
    from doceditor.document_scope import MAX_TEXT_BYTES
    from doceditor.live_store import BrowserMessage

    size = MAX_TEXT_BYTES - 128
    for _ in range(4):
        peer = Doc(client_id=123)
        peer["text"] = Text()
        peer.apply_update(live.doc.get_update())
        before = peer.get_state()
        del peer["text"][0:len(peer["text"])]
        peer["text"].insert(0, "x" * size)
        update = peer.get_update(before)
        if len(update) == MAX_TEXT_BYTES:
            return BrowserMessage("maximum", update)
        size += MAX_TEXT_BYTES - len(update)
    raise AssertionError("Could not construct an exact capped browser backlog")


@pytest.mark.asyncio
async def test_maximum_backlog_three_busy_waits_and_rename_inside_native_join(tmp_path):
    import asyncio
    import time
    from doceditor.live_lane import Operation
    from tests.test_document_live_lane import open_lane

    live, lane = await open_lane(tmp_path)
    assert live.receive(maximum_message(live))
    live.path.write_text("OUTSIDE hello quote")
    # Each real store action still commits; the stub adds its maximal busy
    # wait. This measures the design's three capped waits without racing locks.
    for name in ("commit_import", "record_pending", "promote"):
        original = getattr(live.store, name)

        def delayed(*args, _original=original, **kwargs):
            time.sleep(1.0)
            return _original(*args, **kwargs)

        setattr(live.store, name, delayed)
    started = time.perf_counter()
    result = await lane.submit(Operation("flush"))
    elapsed = time.perf_counter() - started
    assert elapsed < 10
    assert result.server_saved
    assert live.path.read_text() == live.text
    print(f"maximum-backlog-with-three-capped-waits={elapsed:.3f}s")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_maximum_text_flush_empirical_p99(tmp_path):
    import time
    from doceditor.live_lane import Operation
    from tests.test_document_live_lane import open_lane

    live, lane = await open_lane(tmp_path)
    live.receive(maximum_message(live))
    await lane.submit(Operation("flush"))
    timings = []
    for _ in range(100):
        started = time.perf_counter()
        await lane.submit(Operation("flush"))
        timings.append(time.perf_counter() - started)
    p99 = sorted(timings)[98]
    assert p99 <= 1.0
    print(f"maximum-text-flush-p99={p99:.3f}s;samples=100;worst={max(timings):.3f}s")
