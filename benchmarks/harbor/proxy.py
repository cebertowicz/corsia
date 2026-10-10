"""Transparent OpenAI-compatible metering proxy in front of the oMLX endpoint.

Every POST is charged against the unit's call cap before it reaches the
provider; the raw ``usage`` object from the JSON or SSE response is appended to
the meter's JSONL receipt. Other requests (``GET /v1/models``) pass uncharged.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, ClientTimeout, web

from benchmarks.harbor.meter import JsonlMeter, ProviderCallCapExceeded

_HOP_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "accept-encoding"}


def extract_usage(body: bytes, content_type: str) -> dict[str, Any]:
    """Return ``{"usage", "model"}`` from a JSON body or the last SSE event carrying usage."""
    if "text/event-stream" not in content_type:
        try:
            payload = json.loads(body)
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}
    found: dict[str, Any] = {}
    for line in body.decode("utf-8", errors="replace").splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        # Responses API nests the final object under ``response``.
        nested = event.get("response")
        candidate: dict[str, Any] = nested if isinstance(nested, dict) else event
        if isinstance(candidate.get("model"), str):
            found["model"] = candidate["model"]
        if isinstance(candidate.get("usage"), dict):
            found["usage"] = candidate["usage"]
    return found


class MeteringProxy:
    """Serve one unit's metered provider traffic on a loopback port."""

    def __init__(self, meter: JsonlMeter, upstream: str, *, host: str = "127.0.0.1", port: int = 0) -> None:
        self._meter = meter
        self._upstream = upstream.rstrip("/")
        self._host = host
        self._port = port
        self._runner: web.AppRunner | None = None
        self._session: ClientSession | None = None

    @property
    def port(self) -> int:
        """Return the bound port once started."""
        return self._port

    async def start(self) -> None:
        """Bind the proxy; port 0 picks a free port."""
        self._session = ClientSession(timeout=ClientTimeout(total=None, sock_read=1800))
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", self._forward)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        self._port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    async def stop(self) -> None:
        """Close the listener and the upstream session."""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _forward(self, request: web.Request) -> web.StreamResponse:
        charged = request.method == "POST"
        call_index = 0
        if charged:
            try:
                call_index = self._meter.reserve_call()
            except ProviderCallCapExceeded as exc:
                return web.json_response({"error": {"message": str(exc), "type": "call_cap"}}, status=429)
        started = time.monotonic()
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS}
        body = await request.read()
        assert self._session is not None
        async with self._session.request(
            request.method, f"{self._upstream}{request.rel_url}", headers=headers, data=body,
        ) as upstream:
            response = web.StreamResponse(status=upstream.status)
            for key, value in upstream.headers.items():
                if key.lower() not in _HOP_HEADERS and key.lower() != "content-encoding":
                    response.headers[key] = value
            await response.prepare(request)
            buffered = bytearray()
            async for chunk in upstream.content.iter_any():
                buffered.extend(chunk)
                await response.write(chunk)
            await response.write_eof()
            content_type = upstream.headers.get("content-type", "")
            status = upstream.status
        if charged:
            self._meter.record_response(
                call_index, extract_usage(bytes(buffered), content_type), started,
                path=request.rel_url.path, status=status,
            )
        return response


async def _serve(args: argparse.Namespace) -> None:
    meter = JsonlMeter(Path(args.receipt), unit_id=args.unit_id, call_cap=args.call_cap)
    proxy = MeteringProxy(meter, args.upstream, host=args.host, port=args.port)
    await proxy.start()
    print(json.dumps({"port": proxy.port, "receipt": args.receipt}), flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await proxy.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--unit-id", required=True)
    parser.add_argument("--call-cap", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    asyncio.run(_serve(parser.parse_args()))


if __name__ == "__main__":
    main()
