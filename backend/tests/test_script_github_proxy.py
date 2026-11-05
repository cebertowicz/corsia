"""The script GitHub transport forwards only to GitHub, with a synthetic credential."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from automation.script_github_proxy import _serve


class Writer:
    def __init__(self):
        self.data = bytearray()

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


async def _request(host, credential, upstream, target="/user"):
    reader = asyncio.StreamReader()
    reader.feed_data(f"GET {target} HTTP/1.1\r\nHost: {host}\r\nAuthorization: token placeholder\r\n\r\n".encode())
    reader.feed_eof()
    writer = Writer()
    await _serve(reader, writer, credential=credential, transport=httpx.MockTransport(upstream))
    return bytes(writer.data)


@pytest.mark.asyncio
async def test_proxy_injects_account_credential_for_github():
    seen = []

    async def upstream(request):
        seen.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b"ok"))

    response = await _request("api.github.com", lambda: "synthetic-value", upstream)
    assert b"200" in response and b"ok" in response
    assert seen[0].url.host == "api.github.com"
    assert seen[0].headers["authorization"] == "token synthetic-value"


@pytest.mark.asyncio
async def test_proxy_rejects_other_host_without_reading_credential():
    def unexpected():
        raise AssertionError("credential must not be read")

    response = await _request("example.invalid", unexpected, lambda request: httpx.Response(200))
    assert b"403" in response


@pytest.mark.asyncio
async def test_proxy_normalizes_host_and_ignores_absolute_target_host():
    seen = []

    async def upstream(request):
        seen.append(request)
        return httpx.Response(302, headers={"location": "https://example.invalid/"})

    response = await _request("API.GITHUB.COM.:443", lambda: "synthetic-value", upstream,
                              "https://example.invalid/user?q=1")
    assert b"302" in response
    assert len(seen) == 1
    assert str(seen[0].url) == "https://api.github.com/user?q=1"


@pytest.mark.asyncio
async def test_proxy_returns_fixed_failure_without_credential():
    def fail():
        raise ValueError("synthetic-value")

    response = await _request("api.github.com", fail, lambda request: httpx.Response(200))
    assert b"502" in response
    assert b"GitHub request failed" in response
    assert b"synthetic-value" not in response
