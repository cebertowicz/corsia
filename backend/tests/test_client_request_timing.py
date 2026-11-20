"""Browser-side request timing: the Server-Timing route entry, validation, merge and the HTTP route."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.system_perf import router
from infra import analytics_db, client_request_timing as crt, request_timing
from infra.auth import require_user

_TODAY = datetime.now(UTC).date().isoformat()
_BUCKETS = [0, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]


@pytest.fixture()
def analytics(tmp_path, monkeypatch):
    monkeypatch.setattr(analytics_db, "ANALYTICS_DB_PATH", tmp_path / "analytics.duckdb")
    monkeypatch.delenv("CORSIA_ANALYTICS_DISABLE", raising=False)
    analytics_db._reset_for_tests()
    yield analytics_db.get_db()
    analytics_db._reset_for_tests()


def _item(measure="wait", route="/api/threads/{thread_id}", count=2, **over):
    item = {"day": _TODAY, "engine": "webkit", "method": "GET", "route": route, "measure": measure,
            "count": count, "sum_ms": 7.5, "max_ms": 5.0, "buckets": _BUCKETS}
    return {**item, **over}


def _client():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: {}
    return TestClient(app)


def test_server_timing_names_method_and_route_template():
    scope = {"method": "GET", "route": SimpleNamespace(path="/api/threads/{thread_id}")}
    header = request_timing._server_timing(0.0, request_timing.DbTiming(), scope).decode()
    assert header.startswith('route;desc="GET /api/threads/{thread_id}", app;dur=')


def test_server_timing_omits_route_when_unmatched():
    header = request_timing._server_timing(0.0, request_timing.DbTiming(), {"method": "GET"}).decode()
    assert header.startswith("app;dur=")


def test_post_stores_and_merges_rows(analytics):
    client = _client()
    assert client.post("/api/system/perf/client-timing", json={"rows": [_item()]}).json() == {"kept": 1}
    client.post("/api/system/perf/client-timing", json={"rows": [_item(max_ms=9.0)]})

    rows = client.get("/api/system/perf/client-timing?days=1").json()["rows"]
    assert rows == [[_TODAY, "webkit", "GET", "/api/threads/{thread_id}", "wait", 4, 15.0, 9.0,
                     [0, 4] + [0] * 15]]


def test_rows_outside_what_the_page_sends_are_dropped(analytics):
    bad = [
        _item(measure="bogus"), _item(engine="x"), _item(route="no-slash"), _item(day="2001-01-01"),
        _item(day="not-a-day"), _item(count=3), _item(buckets=[1]), _item(route="/" + "a" * 200),
    ]
    good = _item(measure="server")
    assert _client().post("/api/system/perf/client-timing", json={"rows": [*bad, good]}).json() == {"kept": 1}
    assert analytics.execute("SELECT measure FROM client_request_timing_daily").fetchall() == [("server",)]


def test_disabled_analytics_keeps_nothing(analytics, monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")
    assert _client().post("/api/system/perf/client-timing", json={"rows": [_item()]}).json() == {"kept": 0}
    assert analytics.execute("SELECT COUNT(*) FROM client_request_timing_daily").fetchone()[0] == 0


def test_write_failure_is_swallowed(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(crt, "write_rows", boom)
    monkeypatch.delenv("CORSIA_ANALYTICS_DISABLE", raising=False)
    assert _client().post("/api/system/perf/client-timing", json={"rows": [_item()]}).json() == {"kept": 0}


def test_too_many_rows_is_refused():
    rows = [_item(route=f"/api/r{number}") for number in range(crt.MAX_ROWS_PER_BATCH + 1)]
    assert _client().post("/api/system/perf/client-timing", json={"rows": rows}).status_code == 422


def test_retention_prunes_old_client_rows(analytics):
    analytics.execute(
        "INSERT INTO client_request_timing_daily VALUES ('2020-01-01', 'webkit', 'GET', '/old', 'wait', 1, 1, 1, ?)",
        [[1] + [0] * 16],
    )
    request_timing.prune_retention()
    assert analytics.execute("SELECT COUNT(*) FROM client_request_timing_daily").fetchone()[0] == 0
