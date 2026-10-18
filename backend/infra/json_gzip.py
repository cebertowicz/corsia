"""Gzip whole JSON responses for clients that accept it; leave everything else untouched."""

from __future__ import annotations

import asyncio
import gzip

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_MINIMUM_SIZE = 1_000
_COMPRESS_LEVEL = 6


class JsonGZipMiddleware:
    """Compress complete ``application/json`` bodies; pass streams through as written.

    Starlette's ``GZipMiddleware`` also compresses streamed bodies, which buffers
    each write inside the gzip stream; text streams here must reach the client per
    write. Compression runs off the event loop because a large page takes ~100 ms.
    """

    def __init__(self, app: ASGIApp, minimum_size: int = _MINIMUM_SIZE,
                 compresslevel: int = _COMPRESS_LEVEL) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or "gzip" not in Headers(scope=scope).get("accept-encoding", ""):
            await self.app(scope, receive, send)
            return

        held: Message | None = None

        async def send_compressed(message: Message) -> None:
            nonlocal held
            if message["type"] == "http.response.start":
                headers = Headers(raw=message["headers"])
                if (headers.get("content-type", "").startswith("application/json")
                        and "content-encoding" not in headers):
                    held = message
                    return
            elif held is not None and message["type"] == "http.response.body":
                start, held = held, None
                body = message.get("body", b"")
                if not message.get("more_body", False) and len(body) >= self.minimum_size:
                    compressed = await asyncio.to_thread(gzip.compress, body, self.compresslevel)
                    headers = MutableHeaders(raw=start["headers"])
                    headers["Content-Encoding"] = "gzip"
                    headers["Content-Length"] = str(len(compressed))
                    headers.add_vary_header("Accept-Encoding")
                    message = {**message, "body": compressed}
                await send(start)
            await send(message)

        await self.app(scope, receive, send_compressed)
