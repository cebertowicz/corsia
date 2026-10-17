"""The Uvicorn server the Devices listener runs, with an explicit-stop override."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, cast

import uvicorn

if TYPE_CHECKING:
    from infra.devices_listener import DevicesApp

# The one Uvicorn release whose private ``_wait_tasks_to_complete`` the
# override below was written against; any other version uses Uvicorn's own.
_PINNED_UVICORN_VERSION = "0.49.0"


class DevicesServer(uvicorn.Server):
    """A Uvicorn server that never claims process signal handling."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield

    async def _wait_tasks_to_complete(self) -> None:
        if uvicorn.__version__ != _PINNED_UVICORN_VERSION:
            await super()._wait_tasks_to_complete()
            return
        # Uvicorn has already sent normal connection shutdown (including WS
        # code 1012). Active SSE responses have no natural end, so disabling
        # Devices must disconnect transports and join their ASGI requests.
        # A streaming generator can be waiting for its next event rather than
        # reading http.disconnect, so closing the socket alone cannot join it.
        cast("DevicesApp", self.config.app).stopping = True
        for connection in list(self.server_state.connections):
            # TLS close() can wait beyond grace for a peer's close_notify;
            # an explicit Devices stop must also release an unread SSE socket.
            connection.transport.abort()
        await asyncio.sleep(0)
        requests = list(self.server_state.tasks)
        for task in requests:
            task.cancel()
        await asyncio.gather(*requests, return_exceptions=True)
        # These exact owners are now disconnected and joined. Uvicorn's
        # generic waiter would additionally wait for a TLS peer's handshake;
        # keep that external wait out of an explicit Devices stop.
