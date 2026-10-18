"""Tests for the live performance-report endpoint."""

from __future__ import annotations

from datetime import UTC, datetime

import duckdb
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.system_perf import router
from infra.auth import require_user


def test_route_endpoint_uses_own_cursor_while_rw_connection_is_open(monkeypatch, tmp_path):
    """The endpoint can query the analytics DB while its RW owner stays open."""
    conn = duckdb.connect(str(tmp_path / "analytics.duckdb"))
    conn.execute("CREATE TABLE request_timing_daily (day DATE, method VARCHAR, route VARCHAR, status_class VARCHAR, count BIGINT, sum_ms DOUBLE, max_ms DOUBLE, sum_bytes BIGINT, max_bytes BIGINT, buckets INTEGER[])")
    today = datetime.now(UTC).date().isoformat()
    conn.execute("INSERT INTO request_timing_daily VALUES (?, 'GET', '/items/{item_id}', '2xx', 1, 20, 20, 512, 512, [0, 0, 0, 0, 1])", (today,))
    monkeypatch.setattr("infra.analytics_db.get_db", lambda: conn)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: {}

    response = TestClient(app).get("/api/system/perf/routes?days=7")

    assert response.status_code == 200
    assert response.json()["rows"][0][2] == "/items/{item_id}"
    assert response.json()["rows"][0][7:9] == [512, 512]
    conn.close()
