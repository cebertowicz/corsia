"""The Devices listener's server: HTTP/2 or HTTP/1.1 over TLS, with an explicit stop.

A browser opens at most six HTTP/1.1 connections to one origin, so a few slow
reads can hold a POST in the browser's own queue. ALPN offers ``h2`` first, and
HTTP/2 carries every request on one connection; ``http/1.1`` stays for clients
without it.

Hypercorn's public ``serve()`` cannot give this listener its two guarantees:
it would wrap the pre-bound socket's descriptor a second time (``fd://``), and
on shutdown it leaves a streaming response running and waits on each TLS
peer's close_notify. So the accept loop here is this module's own and each
connection is handed to Hypercorn's connection handler, which is not public
API: the pin test fails on any other Hypercorn release.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import sys
from collections.abc import Awaitable, Callable
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from hypercorn.asyncio.tcp_server import TCPServer
from hypercorn.asyncio.worker_context import WorkerContext
from hypercorn.config import Config
from hypercorn.utils import wrap_app

if TYPE_CHECKING:
    from infra.devices_listener import DevicesApp

# The one Hypercorn release whose ``TCPServer`` and ``WorkerContext`` this
# module was verified against (h2 and http/1.1 serving, SSE end on stop, task
# join, WebSocket over HTTP/2 extended CONNECT).
_PINNED_HYPERCORN_VERSION = "0.18.0"
HYPERCORN_VERSION = version("hypercorn")

# Restart, try again: the close code an explicit stop gives an open WebSocket.
_SERVICE_RESTART = 1012
# How long a stop lets the 1012 close frames reach their sockets before it
# disconnects every transport. A peer that is not reading never gets it.
_WEBSOCKET_CLOSE_SECONDS = 0.5
# How long a disconnected connection's handler gets to finish by itself once
# its requests are joined, before it is cancelled.
_CONNECTION_END_SECONDS = 0.5


_access_log = logging.getLogger("uvicorn.access")


def _log_access(scope, status: int) -> None:
    """One access line in Uvicorn's record shape, so the backend log's access formatter and its
    query-string redaction (infra/access_log.py) treat a Devices request like any other."""
    client = scope.get("client")
    path = quote(scope["path"])
    if scope.get("query_string"):
        path = f"{path}?{scope['query_string'].decode('ascii', 'replace')}"
    _access_log.info(
        '%s - "%s %s HTTP/%s" %d',
        "%s:%d" % tuple(client[:2]) if client else "", scope["method"], path, scope["http_version"], status,
    )


class _Requests:
    """Wraps the ASGI app to know each running request and each accepted, still-open WebSocket."""

    def __init__(self, app: DevicesApp):
        self.app = app
        self.tasks: set[asyncio.Task] = set()
        self.websockets: dict[asyncio.Task, Callable[[dict], Awaitable[None]]] = {}

    async def __call__(self, scope, receive, send) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.tasks.add(task)

        async def tracked_send(message: dict) -> None:
            """Send, and note whether this WebSocket is now open or closed."""
            await send(message)
            if message["type"] == "websocket.accept":
                self.websockets[task] = send
            elif message["type"] == "websocket.close":
                self.websockets.pop(task, None)

        async def logged_send(message: dict) -> None:
            """Send, and write the response's access line the way the main server writes its own."""
            await send(message)
            if message["type"] == "http.response.start":
                _log_access(scope, message["status"])

        try:
            await self.app(scope, receive, tracked_send if scope.get("type") == "websocket" else logged_send)
        finally:
            self.tasks.discard(task)
            self.websockets.pop(task, None)

    async def close_websockets(self, code: int) -> None:
        """Send every open WebSocket a close frame with this code, waiting a bounded time for the writes."""
        sends = list(self.websockets.values())
        self.websockets.clear()
        if not sends:
            return
        closing = asyncio.gather(
            *(send({"type": "websocket.close", "code": code}) for send in sends), return_exceptions=True,
        )
        try:
            await asyncio.wait_for(closing, _WEBSOCKET_CLOSE_SECONDS)
        except TimeoutError:
            pass


async def _cancel_and_join(tasks) -> None:
    tasks = list(tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


class DevicesServer:
    """Serves the Devices app on a pre-bound socket; never claims process signal handling."""

    def __init__(self, app: DevicesApp, *, sock: socket.socket, keyfile: Path, certfile: Path):
        self.app = app
        self.sock = sock
        self.config = Config()
        self.config.keyfile = str(keyfile)
        self.config.certfile = str(certfile)
        # Access lines are written by _log_access, in the main server's format.
        self.config.accesslog = None
        # Uvicorn, which this replaces, never closed a connection for its request count; HTTP/2 would
        # otherwise send GOAWAY after 1000 requests and move a long-lived page to a new connection.
        self.config.keep_alive_max_requests = sys.maxsize
        # An existing logger is used as is, so Hypercorn installs no handler of its own.
        self.config.errorlog = logging.getLogger("hypercorn.error")
        self.started = False
        self._exit = asyncio.Event()
        self._requests = _Requests(app)
        self._connections: dict[asyncio.Task, asyncio.StreamWriter] = {}

    @property
    def should_exit(self) -> bool:
        """Whether a stop was requested, so the end of serve() is not reported as a failure."""
        return self._exit.is_set()

    def request_exit(self) -> None:
        """Ask serve() to stop: it ends every connection and returns once they are joined."""
        self._exit.set()

    def _tls_context(self) -> ssl.SSLContext:
        context = self.config.create_ssl_context()
        assert context is not None
        # RFC 9113 section 9.2.1: HTTP/2 over TLS 1.2 must not renegotiate.
        context.options |= ssl.OP_NO_RENEGOTIATION
        return context

    async def serve(self) -> None:
        """Accept TLS connections on the pre-bound socket until request_exit(); raises if the key won't load."""
        tls = self._tls_context()
        context = WorkerContext(None)
        app = wrap_app(self._requests, self.config.wsgi_max_body_size, "asgi")
        loop = asyncio.get_running_loop()
        lifespan_state: dict[str, Any] = {}

        async def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            """Serve one connection with Hypercorn's handler (h2 or http/1.1, as ALPN chose)."""
            if self._exit.is_set():
                # A TLS handshake that finished after the stop began.
                writer.transport.abort()
                return
            task = asyncio.current_task()
            assert task is not None
            self._connections[task] = writer
            try:
                await TCPServer(app, loop, self.config, context, lifespan_state, reader, writer)
            finally:
                self._connections.pop(task, None)

        server = await asyncio.start_server(
            connected, sock=self.sock, ssl=tls, ssl_handshake_timeout=self.config.ssl_handshake_timeout,
        )
        self.started = True
        try:
            await self._exit.wait()
        finally:
            await self._stop(server, context)

    async def _stop(self, server: asyncio.Server, context: WorkerContext) -> None:
        # An open SSE response has no natural end, and a TLS close can wait on
        # a peer's close_notify for as long as the peer likes, so a stop
        # disconnects every transport and joins each ASGI request and then
        # each connection. A streaming generator can be waiting for its next
        # event rather than reading http.disconnect, so closing the socket
        # alone cannot join it.
        self.app.stopping = True
        await context.terminated.set()
        server.close()
        await self._requests.close_websockets(_SERVICE_RESTART)
        for writer in list(self._connections.values()):
            writer.transport.abort()
        await asyncio.sleep(0)
        # Requests first: a connection's handler ends by itself once its
        # requests have ended and its transport is gone. Cancelling the
        # connection while a request is still finishing would have that
        # request schedule work in a task group that is shutting down.
        await _cancel_and_join(self._requests.tasks)
        connections = list(self._connections)
        _, unfinished = await asyncio.wait(connections, timeout=_CONNECTION_END_SECONDS) if connections else ((), ())
        await _cancel_and_join(unfinished)
        # Server.wait_closed() is not awaited: from Python 3.12 it also waits
        # for connections still in their TLS handshake, which an explicit stop
        # does not wait for (connected() refuses them once they finish).
