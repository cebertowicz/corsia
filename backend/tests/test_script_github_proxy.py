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


async def _request(host, credential, upstream, target="/user", **kwargs):
    reader = asyncio.StreamReader()
    reader.feed_data(f"GET {target} HTTP/1.1\r\nHost: {host}\r\nAuthorization: token placeholder\r\n\r\n".encode())
    reader.feed_eof()
    writer = Writer()
    await _serve(reader, writer, credential=credential, transport=httpx.MockTransport(upstream), **kwargs)
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
    assert "transfer-encoding" not in seen[0].headers and seen[0].content == b""
    # A slow GitHub call is bounded by the run's timeout, not httpx's 5-second default.
    assert set(seen[0].extensions["timeout"].values()) == {None}


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
async def test_proxy_streams_request_body_before_it_is_complete():
    reader = asyncio.StreamReader()
    writer = Writer()
    first = asyncio.Event()
    chunks = []

    class StreamingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            async for chunk in request.stream:
                chunks.append(chunk)
                first.set()
            return httpx.Response(200, stream=httpx.ByteStream(b"ok"))

    reader.feed_data(b"POST /upload HTTP/1.1\r\nHost: api.github.com\r\nContent-Length: 6\r\n\r\nabc")
    task = asyncio.create_task(_serve(reader, writer, credential=lambda: "synthetic-value",
                                      transport=StreamingTransport()))
    await asyncio.wait_for(first.wait(), 1)
    assert chunks == [b"abc"]
    reader.feed_data(b"def")
    reader.feed_eof()
    await task
    assert b"".join(chunks) == b"abcdef"
    assert b"200" in writer.data and b"ok" in writer.data


@pytest.mark.asyncio
async def test_proxy_gives_budget_and_client_the_decoded_403_body(monkeypatch):
    # The answer is read whole and decoded, so its saved values can be hidden: the client gets plain bytes
    # with no Content-Encoding, and the forwarder asks GitHub for an uncompressed answer.
    import gzip
    import json

    recorded, seen = [], []

    class Budget:
        def blocked_until(self, low_priority):
            return None

        def record(self, status, headers, body):
            recorded.append((status, body))

    message = {"message": "You have exceeded a secondary rate limit."}
    raw = gzip.compress(json.dumps(message).encode())

    def upstream(request):
        seen.append(request)
        return httpx.Response(403, headers={"content-encoding": "gzip", "content-type": "application/json"},
                              stream=httpx.ByteStream(raw))

    response = await _request("api.github.com", lambda: "synthetic-value", upstream, read_budget=Budget())
    assert recorded == [(403, message)]
    assert seen[0].headers["accept-encoding"] == "identity"
    head, _, body = response.partition(b"\r\n\r\n")
    assert json.loads(body) == message and b"content-encoding" not in head.lower()


@pytest.mark.asyncio
async def test_proxy_withholds_an_answer_over_the_limit():
    from automation.script_github_proxy import OVER_RESPONSE
    from automation.script_tool_channel import REQUEST_LIMIT

    response = await _request("api.github.com", lambda: "synthetic-value",
                              lambda request: httpx.Response(200, content=b"x" * (REQUEST_LIMIT + 1)))
    assert b"502" in response and OVER_RESPONSE in response and b"xxxx" not in response


@pytest.mark.asyncio
async def test_proxy_hides_a_saved_value_in_the_answer(monkeypatch):
    from automation import script_blanking

    monkeypatch.setattr(script_blanking, "saved_values", lambda: frozenset({"saved-secret-value"}))
    response = await _request("api.github.com", lambda: "synthetic-value", lambda request: httpx.Response(
        200, headers={"x-echo": "saved-secret-value"}, content=b'{"echo": "saved-secret-value"}'))
    assert b"200" in response and b"saved-secret-value" not in response


@pytest.mark.asyncio
async def test_proxy_withholds_the_answer_when_saved_values_are_unreadable(monkeypatch):
    from automation import script_blanking
    from automation.script_github_proxy import UNCHECKED

    def unreadable():
        raise script_blanking.Unchecked

    monkeypatch.setattr(script_blanking, "saved_values", unreadable)
    response = await _request("api.github.com", lambda: "synthetic-value",
                              lambda request: httpx.Response(200, content=b"answer"))
    assert b"502" in response and UNCHECKED in response and b"answer" not in response


@pytest.mark.asyncio
async def test_proxy_asks_and_feeds_the_budget_it_was_given():
    from events.connectors.github_read_budget import ReadBudget

    given = ReadBudget(clock=lambda: 1000.0)
    given.record(429, {"retry-after": "60"})

    response = await _request("api.github.com", lambda: "synthetic-value",
                              lambda request: httpx.Response(200), read_budget=given)
    assert b"429" in response and b"paused by the shared rate-limit budget" in response


@pytest.mark.asyncio
async def test_proxy_returns_fixed_failure_without_credential():
    def fail():
        raise ValueError("synthetic-value")

    response = await _request("api.github.com", fail, lambda request: httpx.Response(200))
    assert b"502" in response
    assert b"GitHub request failed" in response
    assert b"synthetic-value" not in response
