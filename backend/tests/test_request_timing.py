"""Tests for aggregate-only request timing instrumentation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import duckdb
import pytest

from infra import request_timing
from infra.db_async import run_in_db


@pytest.fixture(autouse=True)
def _clear_timing_state():
    request_timing._reset_for_tests()
    yield
    request_timing._reset_for_tests()


def test_histogram_percentiles_interpolate_fixed_buckets():
    buckets = [0] * len(request_timing._BUCKET_UPPERS)
    buckets[0], buckets[1] = 2, 2
    assert request_timing.bucket_percentile(buckets, 0.5) == pytest.approx(1.0)
    assert request_timing.bucket_percentile(buckets, 0.95) == pytest.approx(1.9)


def test_route_template_never_uses_raw_path_ids():
    scope = {"path": "/api/threads/secret-id", "route": SimpleNamespace(path="/api/threads/{thread_id}")}
    assert request_timing.route_template(scope) == "/api/threads/{thread_id}"
    assert request_timing.route_template({"path": "/secret-id"}) == "<unmatched>"


@pytest.mark.asyncio
async def test_middleware_preserves_streaming_and_adds_server_timing(monkeypatch):
    recorded = []
    monkeypatch.setattr(request_timing.RequestTimingMiddleware, "_record", lambda *args: recorded.append(args))

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"one", "more_body": True})
        await send({"type": "http.response.body", "body": b"two", "more_body": False})

    sent = []

    async def send(message):
        sent.append(message)

    await request_timing.RequestTimingMiddleware(app)(
        {"type": "http", "method": "GET", "route": SimpleNamespace(path="/stream")}, None, send
    )
    assert [item["body"] for item in sent[1:]] == [b"one", b"two"]
    assert any(name == b"server-timing" for name, _ in sent[0]["headers"])
    assert len(recorded) == 1 and recorded[0][-1] == 6


def test_real_app_records_route_template_and_unhandled_errors():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/items/{item_id}")
    def item(item_id: str):
        return {"id": item_id}

    @app.get("/boom")
    def boom():
        raise RuntimeError("boom")

    app.add_middleware(request_timing.RequestTimingMiddleware)
    client = TestClient(app, raise_server_exceptions=False)
    assert "server-timing" in client.get("/items/secret-id").headers
    assert client.get("/boom").status_code == 500

    keys = {key[1:] for key in request_timing._aggregates}
    assert keys == {("GET", "/items/{item_id}", "2xx"), ("GET", "/boom", "5xx")}
    item = next(value for key, value in request_timing._aggregates.items() if key[2] == "/items/{item_id}")
    boom = next(value for key, value in request_timing._aggregates.items() if key[2] == "/boom")
    assert item.sum_bytes > 0 and item.max_bytes == item.sum_bytes
    assert boom.sum_bytes is None and boom.max_bytes is None


@pytest.mark.asyncio
async def test_websocket_passthrough():
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    await request_timing.RequestTimingMiddleware(app)({"type": "websocket"}, None, None)
    assert seen == ["websocket"]


@pytest.mark.asyncio
async def test_db_pool_timing_accumulates_in_request_context():
    token = request_timing._request_db_timing.set(request_timing.DbTiming())
    try:
        assert await run_in_db(lambda: "ok") == "ok"
        timing = request_timing._request_db_timing.get()
        assert timing is not None and timing.execution_ms >= 0 and timing.queue_ms >= 0
    finally:
        request_timing._request_db_timing.reset(token)
