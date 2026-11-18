"""Two disposable loopback servers: one stands in for the serving CORSIA, one for another instance."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import socket
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

SENTINEL = "OWN_ADDRESS_FIXTURE_OK"
_GIF = base64.b64decode("R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==")
_SERVICE_WORKER = (b"self.addEventListener('message',async e=>{try{const r=await fetch(e.data);"
                   b"e.source.postMessage('read:'+await r.text())}catch(x){e.source.postMessage('refused')}})")
_FRAME = (b"<!doctype html><script>(window.opener||parent).postMessage('reached','*')</script>"
          b"<title>" + SENTINEL.encode() + b"</title>")


@dataclass
class Servers:
    """The two listeners' ports and every request each one received, as (path, is_websocket)."""

    serving: int
    other: int
    requests: dict[str, list[tuple[str, bool]]] = field(default_factory=lambda: {"serving": [], "other": []})


def _response(path: str) -> tuple[str, bytes]:
    if path.startswith("/frame"):
        return "text/html", _FRAME
    if path.endswith(".gif"):
        return "image/gif", _GIF
    if path.endswith(".js"):
        return "application/javascript", (_SERVICE_WORKER if path == "/sw.js" else b"window.__ran=true")
    return "text/html", f"<!doctype html><title>{SENTINEL}</title><body>{SENTINEL}</body>".encode()


async def _serve(reader, writer, requests):
    try:
        lines = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
        path = lines[0].split()[1]
        headers = {k.lower(): v for line in lines[1:] if ": " in line for k, v in [line.split(": ", 1)]}
        websocket = headers.get("upgrade", "").lower() == "websocket"
        requests.append((path, websocket))
        if websocket:
            key = headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
            accept = base64.b64encode(hashlib.sha1(key.encode()).digest()).decode()
            writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          "Sec-WebSocket-Accept: " + accept + "\r\n\r\n").encode())
            await writer.drain()
            await asyncio.wait_for(reader.read(1024), 5)
        elif path.startswith("/redirect?"):
            target = parse_qs(urlsplit(path).query)["target"][0]
            writer.write(f"HTTP/1.1 302 Found\r\nLocation: {target}\r\nContent-Length: 0\r\n"
                         "Connection: close\r\n\r\n".encode())
        else:
            kind, body = _response(path)
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: {kind}\r\nAccess-Control-Allow-Origin: *\r\n"
                         f"Connection: close\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
        await writer.drain()
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
        pass
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()


def ephemeral_pair():
    """Control sockets on free ports: a sandboxed agent shell may not bind the reserved ones."""
    held = [socket.socket(), socket.socket()]
    for listener in held:
        listener.bind(("127.0.0.1", 0))
    return tuple(held)


@contextlib.asynccontextmanager
async def servers():
    """Start both listeners on free ports, on every interface so the machine's own names reach them."""
    result = Servers(0, 0)
    opened = []
    try:
        for label in ("serving", "other"):
            opened.append(await asyncio.start_server(
                lambda r, w, label=label: _serve(r, w, result.requests[label]), "0.0.0.0", 0))
        result.serving, result.other = (item.sockets[0].getsockname()[1] for item in opened)
        yield result
    finally:
        for item in opened:
            item.close()
            await item.wait_closed()
