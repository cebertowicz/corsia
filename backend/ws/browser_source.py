"""Own the pane's upstream connection, including bounded recovery when frames stop."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

import websockets

log = logging.getLogger(__name__)


class StreamConnection(Protocol):
    async def recv(self) -> str | bytes: ...
    async def send(self, message: str) -> None: ...
    async def close(self) -> None: ...


async def _connect(uri: str) -> StreamConnection:
    return await websockets.connect(uri, max_size=None, close_timeout=1)


class BrowserStreamSource:
    """Reconnecting the sole viewer lets agent-browser stop/start its automatic screencast.

    This mitigates missing emissions; it does not establish their upstream cause.
    Static pages can also be quiet, so each pane connection gets at most two restarts.
    The pane socket and its control holder survive; input waits behind the reconnect.
    """

    def __init__(self, uri: str, *, connect: Callable[[str], Awaitable[StreamConnection]] = _connect,
                 idle_timeout: float = 3,
                 reconnect_delay: float = .05, max_restarts: int = 2) -> None:
        self.uri = uri
        self.connect = connect
        self.idle_timeout = idle_timeout
        self.reconnect_delay = reconnect_delay
        self.max_restarts = max_restarts
        self.restarts = 0
        self.connection: StreamConnection | None = None
        self.last_frame = 0.0
        self._send_lock = asyncio.Lock()

    async def __aenter__(self) -> BrowserStreamSource:
        self.connection = await self.connect(self.uri)
        self.last_frame = asyncio.get_running_loop().time()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self.connection is not None:
            await self.connection.close()

    def __aiter__(self) -> BrowserStreamSource:
        return self

    async def __anext__(self) -> str:
        while True:
            timeout: float | None = None
            if self.restarts < self.max_restarts:
                timeout = max(0, self.idle_timeout - (asyncio.get_running_loop().time() - self.last_frame))
            connection = self.connection
            assert connection is not None
            try:
                message = await asyncio.wait_for(connection.recv(), timeout)
            except TimeoutError:
                await self._restart()
                continue
            if isinstance(message, bytes):
                message = message.decode('utf-8')
            try:
                data = json.loads(message)
            except (TypeError, ValueError):
                data = None
            if isinstance(data, dict) and data.get("type") == "frame":
                self.last_frame = asyncio.get_running_loop().time()
            return message

    async def send(self, message: str) -> None:
        async with self._send_lock:
            assert self.connection is not None
            await self.connection.send(message)

    async def _restart(self) -> None:
        async with self._send_lock:
            assert self.connection is not None
            await self.connection.close()
            # Leave no client briefly so the native stream owner can stop the old screencast.
            await asyncio.sleep(self.reconnect_delay)
            self.connection = await self.connect(self.uri)
            self.restarts += 1
            self.last_frame = asyncio.get_running_loop().time()
            log.info("browser stream reconnected after %.1fs without frames (%d/%d)",
                     self.idle_timeout, self.restarts, self.max_restarts)
