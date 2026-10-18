"""Tests for gzip of whole JSON responses, with streams left as written."""

from __future__ import annotations

import asyncio
import gzip
import json

import pytest

from infra.json_gzip import JsonGZipMiddleware

_BIG = {"rows": ["x" * 40] * 200}


def _scope(accept: str = "gzip, deflate") -> dict:
    return {"type": "http", "method": "GET", "path": "/", "headers": [(b"accept-encoding", accept.encode())]}


def _json_app(payload: object):
    body = json.dumps(payload).encode()

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    return app, body


async def _run(app, scope) -> list[dict]:
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    await JsonGZipMiddleware(app)(scope, None, send)
    return sent


@pytest.mark.asyncio
async def test_large_json_is_gzipped_when_the_client_accepts_it():
    app, body = _json_app(_BIG)
    start, message = await _run(app, _scope())
    headers = dict(start["headers"])
    assert headers[b"content-encoding"] == b"gzip"
    assert headers[b"vary"] == b"Accept-Encoding"
    assert int(headers[b"content-length"]) == len(message["body"]) < len(body)
    assert gzip.decompress(message["body"]) == body


@pytest.mark.asyncio
@pytest.mark.parametrize(("payload", "accept"), [(_BIG, "identity"), ({"ok": True}, "gzip")])
async def test_json_stays_plain_without_gzip_or_below_minimum(payload, accept):
    app, body = _json_app(payload)
    start, message = await _run(app, _scope(accept))
    assert b"content-encoding" not in dict(start["headers"])
    assert message["body"] == body


@pytest.mark.asyncio
@pytest.mark.parametrize(("accept", "gzipped"), [
    ("GZIP", True),
    ("br, Gzip;q=0.5", True),
    ("gzip ; Q=1.0", True),
    ("*", True),
    ("gzip;q=0", False),
    ("gzip;q=0.000, br", False),
    ("*;q=1, gzip;q=0", False),
    ("identity", False),
    ("", False),
])
async def test_accept_encoding_is_read_by_coding_and_quality(accept, gzipped):
    app, _ = _json_app(_BIG)
    start, _ = await _run(app, _scope(accept))
    assert (dict(start["headers"]).get(b"content-encoding") == b"gzip") is gzipped


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", [b"text/event-stream", b"text/plain", b"application/json"])
async def test_streams_pass_through_and_flush_per_write(content_type):
    """Each write reaches the client before the app produces the next one."""
    events = [b"data: " + b"y" * 2_000 + b"\n\n", b"data: two\n\n"]
    delivered = asyncio.Event()

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", content_type)]})
        await send({"type": "http.response.body", "body": events[0], "more_body": True})
        await asyncio.wait_for(delivered.wait(), timeout=1)
        await send({"type": "http.response.body", "body": events[1], "more_body": False})

    sent: list[dict] = []

    async def send(message):
        sent.append(message)
        if message.get("body") == events[0]:
            delivered.set()

    await JsonGZipMiddleware(app)(_scope(), None, send)
    assert b"content-encoding" not in dict(sent[0]["headers"])
    assert [message["body"] for message in sent[1:]] == events


@pytest.mark.asyncio
async def test_websocket_scope_is_untouched():
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])
        await send({"type": "websocket.accept"})

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    await JsonGZipMiddleware(app)({"type": "websocket", "headers": [(b"accept-encoding", b"gzip")]}, None, send)
    assert seen == ["websocket"] and sent == [{"type": "websocket.accept"}]


def test_production_app_gzips_json_routes():
    from main import app

    assert any(middleware.cls is JsonGZipMiddleware for middleware in app.user_middleware)
