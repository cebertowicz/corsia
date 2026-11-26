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
# How long a stop waits for the 1012 close frames to be written before it
# disconnects every transport. It is the one wait in a stop that a peer
# controls, and a stop runs on every backend shutdown and every Devices-off,
# whose callers have no timeout of their own.
# Basis: a reading peer has its close frame 3-5 ms after the stop starts, and a
# whole stop took at most 10.3 ms over 50 start/stop cycles (measured on h2 and
# http/1.1), so this is about 100x the measured delivery, which is margin for a
# busy event loop. A close write only waits longer when the peer's TLS write
# buffer is over its high-water mark (512 KiB in asyncio), because it stopped
# reading.
# Too low: such a peer, or one on a stalled loop, gets an abort instead of 1012.
# The web client reconnects the same way after either, so a person sees no
# difference. Too high: every stop with a peer that stopped reading waits this
# long before it disconnects anyone.
_WEBSOCKET_CLOSE_SECONDS = 0.5
# WebSocket keepalive, matching uvicorn's defaults (ping every 20 s, give up 20 s
# after an unanswered ping), which the other listeners still use. Hypercorn has no
# pong timeout: a connection that sends nothing for _SILENT_PEER_SECONDS is closed
# instead. A live peer's pongs reset that clock at least every
# _WEBSOCKET_PING_SECONDS, so a peer that has vanished, or is alive but not
# answering, is cleaned up 20-40 s after the last byte it sent. The cut applies to
# every connection: on http/1.1, one that sends nothing for 40 s is closed even
# with a request still running. On h2, a WebSocket's pongs and the client's
# WINDOW_UPDATE frames keep the shared connection alive.
_WEBSOCKET_PING_SECONDS = 20
_SILENT_PEER_SECONDS = 40


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

        request_read = False

        async def tracked_receive() -> dict:
            nonlocal request_read
            message = await receive()
            if message["type"] != "http.request" or not message.get("more_body", False):
                request_read = True
            return message

        async def logged_send(message: dict) -> None:
            """Send, and write the response's access line the way the main server writes its own."""
            if (message["type"] == "http.response.body" and not message.get("more_body", False)
                    and not request_read and scope.get("http_version") == "1.1"):
                # Hypercorn closes an http/1.1 connection whose response ends before
                # its request body has arrived, so a client that already sent its
                # next request on it gets no response. Uvicorn, which this replaces,
                # sent the response at once, discarded the rest of the body as it
                # arrived, with no cap, and kept the connection; so does this. The
                # response's bytes go out now and only its end waits for the body,
                # which is read and dropped one message at a time. read_timeout ends
                # a sender that stalls, and a stop cancels this like any request.
                if message.get("body"):
                    await send({"type": "http.response.body", "body": message["body"], "more_body": True})
                while not request_read:
                    await tracked_receive()
                message = {"type": "http.response.body", "body": b"", "more_body": False}
            await send(message)
            if message["type"] == "http.response.start":
                _log_access(scope, message["status"])

        try:
            await self.app(
                scope, tracked_receive, tracked_send if scope.get("type") == "websocket" else logged_send,
            )
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
        self.config.websocket_ping_interval = _WEBSOCKET_PING_SECONDS
        self.config.read_timeout = _SILENT_PEER_SECONDS
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
        await _cancel_and_join(self._connections)
        # Server.wait_closed() is not awaited: from Python 3.12 it also waits
        # for connections still in their TLS handshake, which an explicit stop
        # does not wait for (connected() refuses them once they finish).
