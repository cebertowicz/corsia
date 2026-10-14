"""The frame source recovers without replacing the pane connection or losing its input."""
import asyncio

import pytest

from ws.browser_source import BrowserStreamSource


class Connection:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = False

    async def recv(self):
        return await self.incoming.get()

    async def send(self, message):
        self.sent.append(message)

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_no_frame_reconnects_the_upstream_and_keeps_input_on_new_source():
    first, second = Connection(), Connection()
    second.incoming.put_nowait('{"type":"frame","data":"fresh"}')
    connections = iter([first, second])

    async def connect(*args, **kwargs):
        return next(connections)

    async with BrowserStreamSource('ws://127.0.0.1:1234', connect=connect, idle_timeout=.01, reconnect_delay=0) as source:
        assert await anext(source) == '{"type":"frame","data":"fresh"}'
        assert first.closed
        await source.send('input')
        assert second.sent == ['input']
        assert source.restarts == 1
    assert second.closed


@pytest.mark.asyncio
async def test_status_messages_do_not_hide_missing_frames_and_restarts_are_bounded():
    connections = [Connection() for _ in range(3)]
    for connection in connections:
        connection.incoming.put_nowait('{"type":"status"}')
    available = iter(connections)

    async def connect(*args, **kwargs):
        return next(available)

    async with BrowserStreamSource('ws://127.0.0.1:1234', connect=connect, idle_timeout=.01, reconnect_delay=0) as source:
        for _ in range(3):
            assert await anext(source) == '{"type":"status"}'
        reader = asyncio.create_task(anext(source))
        await asyncio.sleep(.03)
        assert not reader.done()
        assert source.restarts == 2
        connections[-1].incoming.put_nowait('{"type":"frame"}')
        assert await reader == '{"type":"frame"}'
