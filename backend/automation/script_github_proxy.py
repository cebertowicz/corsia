"""Per-run GitHub HTTP transport for authenticated sandboxed scripts."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import h11
import httpx

from events.connectors.github_read_budget import budget

_ALLOWED_HOSTS = frozenset({b"api.github.com", b"uploads.github.com"})
_HOP_HEADERS = frozenset({b"connection", b"transfer-encoding", b"content-length", b"host", b"authorization"})


async def _serve(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *,
    credential: Callable[[], str], transport: httpx.AsyncBaseTransport | None,
) -> None:
    conn = h11.Connection(h11.SERVER)
    started = False
    try:
        request = None
        body = bytearray()
        while True:
            event = conn.next_event()
            if event is h11.NEED_DATA:
                data = await reader.read(65536)
                conn.receive_data(data)
                if not data:
                    return
            elif isinstance(event, h11.Request):
                request = event
            elif isinstance(event, h11.Data):
                body.extend(event.data)
            elif isinstance(event, h11.EndOfMessage):
                break
            elif isinstance(event, h11.ConnectionClosed):
                return
        if request is None:
            return
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
            writer.write(conn.send(h11.Response(status_code=403, headers=[(b"content-length", b"0")])))
            writer.write(conn.send(h11.EndOfMessage()))
            await writer.drain()
            return
        if request.method in (b"GET", b"HEAD") and budget.blocked_until(low_priority=True) is not None:
            message = b"GitHub reads are paused by the shared rate-limit budget\n"
            writer.write(conn.send(h11.Response(status_code=429, headers=[
                (b"content-length", str(len(message)).encode()), (b"connection", b"close"),
            ])))
            writer.write(conn.send(h11.Data(data=message)))
            writer.write(conn.send(h11.EndOfMessage()))
            await writer.drain()
            return
        headers = [(name, value) for name, value in request.headers if name.lower() not in _HOP_HEADERS]
        headers.append((b"authorization", b"token " + credential().encode("utf-8")))
        url = "https://" + host.decode("ascii") + target
        async with httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False) as client:
            async with client.stream(request.method.decode("ascii"), url, headers=headers, content=bytes(body)) as response:
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
    except Exception:  # The client gets fixed text, never request or credential details.
        if not started:
            try:
                message = b"GitHub request failed\n"
                writer.write(conn.send(h11.Response(status_code=502, headers=[
                    (b"content-length", str(len(message)).encode()), (b"connection", b"close"),
                ])))
                writer.write(conn.send(h11.Data(data=message)))
                writer.write(conn.send(h11.EndOfMessage()))
                await writer.drain()
            except Exception:
                pass
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
