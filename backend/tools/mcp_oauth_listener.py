"""The one-time loopback callback listener for an MCP sign-in.

Binds ``127.0.0.1`` only, never ``0.0.0.0`` or ``::``. It answers a GET on the
callback path whose ``Host`` is exactly this listener, and refuses everything
else without ending the flow: any process on this Mac can reach the port, so a
junk request must not cancel the person's sign-in. It stops after a valid
callback, the caller's expiry, or a small request cap.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from html import escape
from urllib.parse import parse_qs, urlsplit

BIND_HOST = "127.0.0.1"
REQUEST_CAP = 25
_HEAD_LIMIT = 8192
_READ_TIMEOUT = 5.0
_QUERY_FIELDS = 12
_VALUE_MAX = 2048

#: handler(query) -> (status, page text, finished, refusal class or None). ``finished`` stops the listener
#: after this reply; a refusal class is counted and the flow keeps listening.
Handler = Callable[[dict[str, str]], Awaitable[tuple[int, str, bool, str | None]]]
CapReached = Callable[[], Awaitable[None]]


def _page(title: str, message: str) -> bytes:
    body = (
        '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        f"<title>{escape(title)}</title><body style=\"font:16px system-ui;margin:3rem auto;max-width:32rem;padding:0 1rem\">"
        f"<p>{escape(message)}</p></body>"
    )
    return body.encode()


def _response(status: int, body: bytes) -> bytes:
    reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed", 431: "Request Header Fields Too Large"}.get(status, "OK")
    head = (
        f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nX-Content-Type-Options: nosniff\r\n"
        "Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'\r\nConnection: close\r\n\r\n"
    )
    return head.encode() + body


class CallbackListener:
    """A loopback server for one sign-in; ``requests`` counts every connection it served."""

    def __init__(self, *, path: str, allowed_hosts: Callable[[int], set[str]], handler: Handler, on_cap: CapReached,
                 port: int = 0) -> None:
        self._path, self._allowed_hosts, self._handler, self._on_cap = path, allowed_hosts, handler, on_cap
        self._requested_port = port
        self._server: asyncio.Server | None = None
        self.port = 0
        self.requests = 0
        self.refused: dict[str, int] = {}
        self._stopped = False

    async def start(self) -> int:
        self._server = await asyncio.start_server(
            self._serve, host=BIND_HOST, port=self._requested_port, limit=_HEAD_LIMIT, reuse_address=True,
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    def bound_addresses(self) -> list[str]:
        """Every address the server listens on. For tests that prove loopback-only binding."""
        return [sock.getsockname()[0] for sock in (self._server.sockets if self._server else [])]

    async def stop(self) -> None:
        self._stopped = True
        if self._server is not None:
            self._server.close()
            self._server = None

    def _refuse(self, kind: str) -> None:
        self.refused[kind] = self.refused.get(kind, 0) + 1

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.requests += 1
        try:
            reply, finished = await self._answer(reader)
            writer.write(reply)
            await asyncio.wait_for(writer.drain(), _READ_TIMEOUT)
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            self._refuse("malformed")
            finished = False
        finally:
            writer.close()
        if finished:
            await self.stop()
        elif self.requests >= REQUEST_CAP and not self._stopped:
            await self.stop()
            await self._on_cap()

    async def _answer(self, reader: asyncio.StreamReader) -> tuple[bytes, bool]:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), _READ_TIMEOUT)
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        hosts = [line.split(":", 1)[1].strip() for line in lines[1:] if line.lower().startswith("host:")]
        if len(parts) != 3 or len(hosts) != 1:
            self._refuse("malformed")
            return _response(400, _page("CORSIA", "Not a sign-in request.")), False
        method, target, host = parts[0], parts[1], hosts[0].lower()
        if host not in self._allowed_hosts(self.port):
            self._refuse("host")
            return _response(400, _page("CORSIA", "Not a sign-in request.")), False
        if method != "GET":
            self._refuse("method")
            return _response(405, _page("CORSIA", "Not a sign-in request.")), False
        url = urlsplit(target)
        if url.path != self._path:
            self._refuse("path")
            return _response(404, _page("CORSIA", "Not a sign-in request.")), False
        query = {
            key: values[0][:_VALUE_MAX]
            for key, values in list(parse_qs(url.query, keep_blank_values=True).items())[:_QUERY_FIELDS] if values
        }
        status, text, finished, refusal = await self._handler(query)
        if refusal:
            self._refuse(refusal)
        return _response(status, _page("CORSIA", text)), finished
