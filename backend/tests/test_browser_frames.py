"""A blocked pane retains one pending image and every reliable message."""
import asyncio
import contextlib
import json

import pytest

from ws.browser_frames import PaneFrames


@pytest.mark.asyncio
async def test_slow_writer_replaces_pending_frames_without_dropping_control():
    frames = PaneFrames()
    blocked = asyncio.Event()
    sending = asyncio.Event()
    delivered = asyncio.Event()
    sent = []

    async def send(message):
        sent.append(json.loads(message))
        if len(sent) == 1:
            sending.set()
            await blocked.wait()
        if len(sent) == 4:
            delivered.set()

    frames.offer('{"type":"frame","data":"first"}')
    writer = asyncio.create_task(frames.send(send))
    try:
        await asyncio.wait_for(sending.wait(), 1)
        for index in range(100):
            frames.offer(json.dumps({"type": "frame", "data": index}))
        frames.control({"type": "control", "has_control": True})
        frames.control({"type": "navigate_result", "error": None})
        blocked.set()
        await asyncio.wait_for(delivered.wait(), 1)
        assert sent == [
            {"type": "frame", "data": "first"},
            {"type": "control", "has_control": True},
            {"type": "navigate_result", "error": None},
            {"type": "frame", "data": 99},
        ]
    finally:
        writer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await writer


@pytest.mark.asyncio
async def test_idle_writer_wakes_for_next_reliable_message():
    frames = PaneFrames()
    sent = asyncio.Queue()
    writer = asyncio.create_task(frames.send(sent.put))
    try:
        frames.control({"type": "browser_state", "state": "error"})
        assert json.loads(await asyncio.wait_for(sent.get(), 1)) == {"type": "browser_state", "state": "error"}
        await asyncio.sleep(0)
        frames.control({"type": "control", "has_control": False})
        assert json.loads(await asyncio.wait_for(sent.get(), 1)) == {"type": "control", "has_control": False}
    finally:
        writer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await writer
