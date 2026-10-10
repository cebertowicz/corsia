from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, Request

from infra.local_connection import is_direct_loopback


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/probe")
    async def probe(request: Request):
        return {"direct": is_direct_loopback(request)}

    return app


async def _probe(host: str, headers: dict[str, str] | None = None) -> bool:
    transport = httpx.ASGITransport(app=_app(), client=(host, 9000))
    async with httpx.AsyncClient(transport=transport, base_url="http://corsia") as client:
        return (await client.get("/probe", headers=headers)).json()["direct"]


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
async def test_plain_loopback_connection_is_local(host):
    assert await _probe(host) is True


@pytest.mark.asyncio
async def test_non_loopback_peer_is_not_local():
    assert await _probe("192.0.2.1") is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"Tailscale-User-Login": "person@example.com"},
        {"X-Forwarded-For": "100.64.0.9"},
        {"Forwarded": "for=100.64.0.9"},
        {"X-Real-IP": "100.64.0.9"},
    ],
)
async def test_forwarded_loopback_request_is_never_local(headers):
    assert await _probe("127.0.0.1", headers) is False
