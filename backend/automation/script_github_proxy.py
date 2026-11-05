"""Per-run GitHub HTTP transport for authenticated sandboxed scripts."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import h11
import httpx

from events.connectors.github_read_budget import budget

logger = logging.getLogger(__name__)

_ALLOWED_HOSTS = frozenset({b"api.github.com", b"uploads.github.com"})
_HOP_HEADERS = frozenset({b"connection", b"transfer-encoding", b"content-length", b"host", b"authorization"})


async def _read_request(reader: asyncio.StreamReader, conn: h11.Connection):
    """The one request on this connection and its body, or ``None`` if it ended first."""
    request = None
    body = bytearray()
    while True:
        event = conn.next_event()
        if event is h11.NEED_DATA:
            data = await reader.read(65536)
            conn.receive_data(data)
            if not data:
                return None
        elif isinstance(event, h11.Request):
            request = event
        elif isinstance(event, h11.Data):
            body.extend(event.data)
        elif isinstance(event, h11.EndOfMessage):
            return (request, bytes(body)) if request is not None else None
        elif isinstance(event, h11.ConnectionClosed):
            return None


def _github_target(request: h11.Request) -> tuple[bytes, str] | None:
    """The normalized GitHub host and origin-form target, or ``None`` for anything else."""
    hosts = [value.lower() for name, value in request.headers if name.lower() == b"host"]
    host = hosts[0] if len(hosts) == 1 else b""
    if host.endswith(b":443"):
        host = host[:-4]
    host = host.removesuffix(b".")
    target = request.target.decode("ascii")
    if target.startswith(("http://", "https://")):
        parsed = urlsplit(target)
        target = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
    if host not in _ALLOWED_HOSTS or not target.startswith("/"):
        return None
    return host, target


async def _reply(conn: h11.Connection, writer: asyncio.StreamWriter, status: int, message: bytes) -> None:
    writer.write(conn.send(h11.Response(status_code=status, headers=[
        (b"content-length", str(len(message)).encode()), (b"connection", b"close"),
    ])))
    if message:
        writer.write(conn.send(h11.Data(data=message)))
    writer.write(conn.send(h11.EndOfMessage()))
    await writer.drain()


async def _serve(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *,
    credential: Callable[[], str], transport: httpx.AsyncBaseTransport | None,
) -> None:
    conn = h11.Connection(h11.SERVER)
    started = False
    try:
        received = await _read_request(reader, conn)
        if received is None:
            return
        request, body = received
        github = _github_target(request)
        if github is None:
            await _reply(conn, writer, 403, b"")
            return
        host, target = github
        if request.method in (b"GET", b"HEAD") and budget.blocked_until(low_priority=True) is not None:
            await _reply(conn, writer, 429, b"GitHub reads are paused by the shared rate-limit budget\n")
            return
        headers = [(name, value) for name, value in request.headers if name.lower() not in _HOP_HEADERS]
        headers.append((b"authorization", b"token " + credential().encode("utf-8")))
        url = "https://" + host.decode("ascii") + target
        async with httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False) as client:
            async with client.stream(request.method.decode("ascii"), url, headers=headers, content=body) as response:
                response_headers = [(name, value) for name, value in response.headers.raw
                                    if name.lower() not in _HOP_HEADERS]
                response_headers.append((b"connection", b"close"))
                writer.write(conn.send(h11.Response(status_code=response.status_code, headers=response_headers)))
                started = True
                limited_body = bytearray()
                async for chunk in response.aiter_raw():
                    if response.status_code == 403:
                        limited_body.extend(chunk)
                    writer.write(conn.send(h11.Data(data=chunk)))
                    await writer.drain()
                try:
                    parsed_body = json.loads(limited_body) if limited_body else None
                except (ValueError, UnicodeDecodeError):
                    parsed_body = None
                budget.record(response.status_code, dict(response.headers), parsed_body)
                writer.write(conn.send(h11.EndOfMessage()))
                await writer.drain()
    except Exception as exc:  # The client gets fixed text, never request or credential details.
        # Only the exception type is logged: its message could carry the request or the credential.
        logger.warning("script GitHub forwarder request failed: %s", type(exc).__name__)
        if not started:
            try:
                await _reply(conn, writer, 502, b"GitHub request failed\n")
            except (OSError, h11.LocalProtocolError):
                logger.debug("script GitHub forwarder: client gone before the failure reply")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


@asynccontextmanager
async def github_proxy(path: Path, credential: Callable[[], str], *,
                       transport: httpx.AsyncBaseTransport | None = None):
    """Serve GitHub requests until the associated script run exits."""
    active: set[asyncio.Task] = set()

    def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(_serve(reader, writer, credential=credential, transport=transport))
        active.add(task)
        task.add_done_callback(active.discard)

    server = await asyncio.start_unix_server(connected, path=str(path))
    try:
        path.chmod(0o600)
        yield
    finally:
        server.close()
        await server.wait_closed()
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        path.unlink(missing_ok=True)
