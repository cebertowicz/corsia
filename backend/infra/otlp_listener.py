"""Loopback listener for the OTLP/HTTP logs receiver, kept apart from the main app's port."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import os
import socket

import uvicorn

from infra import otlp_state
from infra.otlp_receiver import OtlpReceiver

DEFAULT_PORT = 4318  # the standard OTLP/HTTP port, so an exporter's default endpoint works
FALLBACK_PORTS = range(4319, 4328)
PORT_ENV = "CORSIA_OTLP_PORT"
logger = logging.getLogger(__name__)


class _OtlpServer(uvicorn.Server):
    """A Uvicorn server that never claims process signal handling."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


def _ports() -> tuple[int, ...]:
    """The ports to try: only the override when it is set, else the default and its fallbacks."""
    override = os.environ.get(PORT_ENV, "")
    if override.isdigit():
        return (int(override),)
    return (DEFAULT_PORT, *FALLBACK_PORTS)


def _listening(port: int) -> bool:
    """Whether something already accepts on loopback at ``port``.

    Catches a collector bound to every address, which macOS lets SO_REUSEADDR bind loopback over,
    silently taking its traffic. A port held only by TIME_WAIT refuses the connect, so it is reused.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _prebind() -> socket.socket | None:
    for port in _ports():
        if _listening(port):
            continue
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
            sock.listen(socket.SOMAXCONN)
            sock.setblocking(False)
            return sock
        except OSError as exc:
            sock.close()
            if exc.errno != errno.EADDRINUSE:
                logger.warning("OTLP listener could not bind port %d: %s", port, exc.strerror)
                return None
    logger.warning("OTLP listener found no free port; outside tools cannot send measures")
    return None


class OtlpListener:
    """Own the receiver's server, its pre-bound socket and the published port."""

    def __init__(self) -> None:
        self._server: _OtlpServer | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> bool:
        if self._task is not None:
            return True
        sock = _prebind()
        if sock is None:
            return False
        port = sock.getsockname()[1]
        config = uvicorn.Config(
            OtlpReceiver(), host="127.0.0.1", port=port, log_config=None, proxy_headers=False,
            lifespan="off", timeout_graceful_shutdown=2,
        )
        self._server = _OtlpServer(config)
        self._task = asyncio.create_task(self._server.serve(sockets=[sock]))
        self._task.add_done_callback(self._serve_ended)
        otlp_state.set_port(port)
        return True

    def _serve_ended(self, task: asyncio.Task) -> None:
        if task is not self._task or task.cancelled() or (self._server is not None and self._server.should_exit):
            return
        logger.error("OTLP listener stopped serving: %s", task.exception() or "the server exited")
        otlp_state.set_port(None)
        self._server = self._task = None

    async def stop(self) -> None:
        if self._server is None or self._task is None:
            return
        self._server.should_exit = True
        try:
            await self._task
        finally:
            otlp_state.set_port(None)
            self._server = self._task = None


listener = OtlpListener()
