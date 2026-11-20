"""Browser-side request timing: the Server-Timing route entry, validation, merge, the registry and the HTTP routes."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from loguru import logger

from api.system_perf import router
from infra import analytics_db, client_request_timing as crt, request_timing
from infra.auth import require_user

_TODAY = datetime.now(UTC).date().isoformat()
_BUCKETS = [0, 2] + [0] * 15
_ROUTE = "/api/system/perf/routes"


@pytest.fixture()
def analytics(tmp_path, monkeypatch):
    monkeypatch.setattr(analytics_db, "ANALYTICS_DB_PATH", tmp_path / "analytics.duckdb")
    monkeypatch.delenv("CORSIA_ANALYTICS_DISABLE", raising=False)
    analytics_db._reset_for_tests()
    crt._reset_for_tests()
    yield analytics_db.get_db()
    analytics_db._reset_for_tests()


@pytest.fixture()
def warnings():
    """The loguru WARNING lines emitted during a test."""
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="WARNING", format="{message}")
    yield lines
    logger.remove(sink)


def _item(measure="wait", route=_ROUTE, count=2, **over):
    item = {"day": _TODAY, "engine": "webkit", "method": "GET", "route": route, "measure": measure,
            "count": count, "sum_ms": 7.5, "max_ms": 5.0, "buckets": _BUCKETS}
    return {**item, **over}


def _client():
    app = FastAPI()
    app.add_middleware(request_timing.RequestTimingMiddleware)
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: {}
    return TestClient(app)


def _post(client, rows, engine="webkit", issues=None):
    body = {"engine": engine, "rows": rows, **({"issues": issues} if issues is not None else {})}
    return client.post("/api/system/perf/client-timing", json=body)


def test_server_timing_names_method_and_route_template():
    scope = {"method": "GET", "route": SimpleNamespace(path="/api/threads/{thread_id}")}
    header = request_timing._server_timing(0.0, request_timing.DbTiming(), scope).decode()
    assert header.startswith('route;desc="GET /api/threads/{thread_id}", app;dur=')


def test_server_timing_omits_route_when_unmatched():
    header = request_timing._server_timing(0.0, request_timing.DbTiming(), {"method": "GET"}).decode()
    assert header.startswith("app;dur=")


def test_route_the_server_names_is_a_registered_template():
    """What the page sends back is exactly what the registry accepts, including for an included router."""
    app = FastAPI()
    app.add_middleware(request_timing.RequestTimingMiddleware)
    inner = APIRouter(prefix="/api/things")

    @inner.get("/{thing_id}/parts/{part:path}")
    def part(thing_id: str, part: str):
        return {}

    app.include_router(inner)

    @app.get("/api/direct/{item}")
    def direct(item: str):
        return {}

    client = TestClient(app)
    templates = crt.registered_route_templates(app)
    for path in ("/api/things/a1/parts/x/y", "/api/direct/z"):
        header = client.get(path).headers["server-timing"]
        desc = re.search(r'route;desc="GET ([^"]+)"', header).group(1)
        assert desc in templates
    assert templates == {"/api/things/{thing_id}/parts/{part:path}", "/api/direct/{item}"}


def test_real_app_route_templates_fit_the_route_limit():
    from main import app

    templates = crt.registered_route_templates(app)
    assert "/api/system/perf/client-timing" in templates
    assert len(templates) > 100
    assert max(map(len, templates)) <= 77  # the length the route limit's comment states
    assert 77 <= crt.ROUTE_MAX_LENGTH


def test_post_stores_and_merges_rows(analytics):
    client = _client()
    assert _post(client, [_item()]).json() == {"kept": 1, "dropped": 0}
    _post(client, [_item(max_ms=9.0)])

    rows = client.get("/api/system/perf/client-timing?days=1").json()["rows"]
    assert rows == [[_TODAY, "webkit", "GET", _ROUTE, "wait", 4, 15.0, 9.0, [0, 4] + [0] * 15]]


def test_a_route_that_is_not_a_registered_template_is_dropped_and_counted(analytics):
    rows = [
        _item(route=_ROUTE),  # registered
        _item(route="/api/threads/abc123"),  # a concrete path with an id
        _item(route="/api/nope/{x}"),  # template-shaped but not registered
        _item(route=crt.UNKNOWN_ROUTE, measure="total"),  # the page's no-route key
    ]
    assert _post(_client(), rows).json() == {"kept": 2, "dropped": 2}
    stored = analytics.execute("SELECT route FROM client_request_timing_daily ORDER BY route").fetchall()
    assert stored == [("/<unknown>",), (_ROUTE,)]


def test_rows_outside_what_the_page_sends_are_dropped_or_refused(analytics):
    client = _client()
    out_of_range = [_item(day="2001-01-01"), _item(count=3)]  # an old day; buckets that do not sum to count
    assert _post(client, out_of_range).json() == {"kept": 0, "dropped": 2}
    for bad in (_item(measure="bogus"), _item(engine="x"), _item(buckets=[1]), _item(day="not-a-day"),
                _item(route="/" + "a" * (crt.ROUTE_MAX_LENGTH + 1)), _item(buckets=[-1, 3] + [0] * 15)):
        assert _post(client, [bad]).status_code == 422
    assert analytics.execute("SELECT COUNT(*) FROM client_request_timing_daily").fetchone()[0] == 0


def test_a_batch_over_the_row_limit_is_refused():
    rows = [_item(measure="wait")] * (crt.BATCH_ROWS + 1)
    assert _post(_client(), rows).status_code == 422


def test_disabled_analytics_keeps_nothing_drops_nothing_and_warns_of_nothing(analytics, monkeypatch, warnings):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")
    assert _post(_client(), [_item()], issues={"observer_install_failed": 1}).json() == {"kept": 0, "dropped": 0}
    assert not [w for w in warnings if "client request timing" in w]
    assert analytics.execute("SELECT COUNT(*) FROM client_request_timing_daily").fetchone()[0] == 0


def test_write_failure_is_swallowed_and_logged(monkeypatch, warnings):
    def boom(*_args, **_kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(crt, "write_rows", boom)
    monkeypatch.delenv("CORSIA_ANALYTICS_DISABLE", raising=False)
    assert _post(_client(), [_item()]).json() == {"kept": 0, "dropped": 1}
    assert any("write failed" in line for line in warnings)


def test_issues_are_validated_capped_and_logged_once_per_post(analytics, warnings):
    issues = {"observer_install_failed": 1, "post_failed:503": 5_000_000, "post_failed:network": 2,
              "post_failed:99999": 1, "made_up": 4, "server_dropped": -3}
    assert _post(_client(), [], engine="webkit", issues=issues).json() == {"kept": 0, "dropped": 0}
    [line] = [w for w in warnings if "client request timing:" in w]
    assert "engine=webkit" in line and "posts_from_engine=1" in line
    assert "'observer_install_failed': 1" in line
    assert f"'post_failed:503': {crt.ISSUE_COUNT_CAP}" in line
    assert "'post_failed:network': 2" in line and "'server_dropped': 0" in line
    assert "made_up" not in line and "99999" not in line


def test_a_batch_of_too_many_issue_keys_is_refused():
    issues = {f"post_failed:{500 + n}": 1 for n in range(crt.MAX_ISSUE_KEYS + 1)}
    assert _post(_client(), [], issues=issues).status_code == 422


def test_dropped_rows_log_a_warning_with_the_engine_counts(analytics, warnings):
    client = _client()
    _post(client, [_item(route="/api/threads/abc")], engine="webkit")
    _post(client, [_item()], engine="webkit")  # clean: no warning
    _post(client, [_item(route="/api/threads/abc")], engine="chromium")
    lines = [w for w in warnings if "client request timing:" in w]
    assert len(lines) == 2
    assert "engine=webkit sent=1 kept=0 dropped=1" in lines[0] and "posts_from_engine=1" in lines[0]
    assert "engine=chromium" in lines[1] and "posts_from_engine=1" in lines[1]


def test_get_window_is_capped_at_the_retention():
    client = _client()
    assert client.get(f"/api/system/perf/client-timing?days={crt.MAX_WINDOW_DAYS + 1}").status_code == 422


def test_retention_prunes_old_client_rows(analytics):
    old = (datetime.now(UTC) - timedelta(days=crt.MAX_WINDOW_DAYS + 1)).date()
    for day, route in ((old, "/old"), (datetime.now(UTC).date(), "/new")):
        analytics.execute(
            "INSERT INTO client_request_timing_daily VALUES (?, 'webkit', 'GET', ?, 'wait', 1, 1, 1, ?)",
            [day, route, [1] + [0] * 16],
        )
    request_timing.prune_retention()
    assert analytics.execute("SELECT route FROM client_request_timing_daily").fetchall() == [("/new",)]


def test_the_page_names_the_same_unknown_route_and_buckets_as_the_server():
    """The page's copies of the server's constants are the server's, so a change to either side fails here."""
    source = (Path(__file__).resolve().parents[2] / "frontend/src/lib/api/clientTiming.ts").read_text()
    assert f"UNKNOWN_ROUTE = '{crt.UNKNOWN_ROUTE}'" in source
    assert "BUCKET_UPPERS = STATS_BUCKET_UPPERS" in source  # the list itself is tied to the server's by vitest
    assert f"BATCH_ROWS = {crt.BATCH_ROWS};" in source
    assert f"ISSUE_COUNT_CAP = {crt.ISSUE_COUNT_CAP:_};" in source
    assert f"MAX_ISSUE_KEYS = {crt.MAX_ISSUE_KEYS};" in source
    for kind in crt.ISSUE_KINDS:
        assert kind.replace(":network", "") in source
