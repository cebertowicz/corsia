"""The outcome of one live-document frame written to a socket."""
from __future__ import annotations

import asyncio
import logging

from fastapi import WebSocketDisconnect

log = logging.getLogger("ws.document_control")


_CLOSED_SOCKET_SEND_ERRORS = {
    "Unexpected ASGI message 'websocket.send', after sending 'websocket.close'",
    "Unexpected ASGI message 'websocket.send', after sending 'websocket.close' or response already completed.",
    'Cannot call "send" once a close message has been sent.',
}


def closed_socket_error(error: BaseException) -> bool:
    """Whether document delivery lost a socket after its work already completed."""
    return isinstance(error, (WebSocketDisconnect, ConnectionError)) or (
        isinstance(error, RuntimeError) and str(error) in _CLOSED_SOCKET_SEND_ERRORS)


def observe_send(task: asyncio.Task) -> None:
    """Retrieve a broadcast write's outcome; a socket that closed first is expected."""
    if task.cancelled() or (error := task.exception()) is None:
        return
    if closed_socket_error(error):
        log.debug("Live-document frame not delivered to a closed socket: %s", error)
    else:
        log.warning("Live-document frame not delivered", exc_info=error)
