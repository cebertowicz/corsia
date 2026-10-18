"""Request timing persistence against a real DuckDB analytics file."""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from infra import analytics_db, request_timing

_KEY = ("2026-09-25", "GET", "/x", "2xx")


@pytest.fixture()
def analytics(tmp_path, monkeypatch):
    """Point the process-wide analytics connection at a bootstrapped temp file."""
    monkeypatch.setattr(analytics_db, "ANALYTICS_DB_PATH", tmp_path / "analytics.duckdb")
    monkeypatch.delenv("CORSIA_ANALYTICS_DISABLE", raising=False)
    analytics_db._reset_for_tests()
    request_timing._reset_for_tests()
    yield analytics_db.get_db()
    request_timing._reset_for_tests()
    analytics_db._reset_for_tests()


def _aggregate(*durations: float, response_bytes: int = 0) -> request_timing.Aggregate:
    aggregate = request_timing.Aggregate()
    for duration in durations:
        aggregate.add(duration, response_bytes)
    return aggregate


def _slow(route: str = "/x") -> request_timing.SlowRequest:
    return request_timing.SlowRequest(datetime.now(UTC), "GET", route, 200, 1_200, 2, 3)


def _record(route: str = "/x") -> None:
    request_timing.RequestTimingMiddleware._record(
        {"method": "GET", "route": SimpleNamespace(path=route)}, 0, 200, request_timing.DbTiming()
    )


def test_write_batch_merges_into_existing_daily_row(analytics):
    request_timing.write_batch({_KEY: _aggregate(1, 300, response_bytes=100)}, [])
    request_timing.write_batch({_KEY: _aggregate(1, 5_000, response_bytes=400)}, [])

    count, sum_ms, max_ms, sum_bytes, max_bytes, buckets = analytics.execute(
        "SELECT count, sum_ms, max_ms, sum_bytes, max_bytes, buckets FROM request_timing_daily"
    ).fetchone()
    expected = [left + right for left, right in zip(_aggregate(1, 300).buckets, _aggregate(1, 5_000).buckets, strict=True)]
    assert (count, sum_ms, max_ms, sum_bytes, max_bytes) == (4, 5_302, 5_000, 1_000, 400)
    assert buckets == expected


def test_write_batch_inserts_slow_rows(analytics):
    request_timing.write_batch({}, [_slow("/a"), _slow("/b")])

    rows = analytics.execute("SELECT route, status, dur_ms, dbq_ms, db_ms, response_bytes FROM request_slow ORDER BY route").fetchall()
    assert rows == [("/a", 200, 1_200, 2, 3, 0), ("/b", 200, 1_200, 2, 3, 0)]


def test_write_batch_persists_per_lane_columns_and_leaves_unmeasured_null(analytics):
    laned = request_timing.SlowRequest(
        datetime.now(UTC), "GET", "/laned", 200, 1_200, 9, 8, response_bytes=4,
        dbq_read_ms=3, db_read_ms=2, dbq_bulk_ms=1, db_bulk_ms=0,
    )
    request_timing.write_batch({}, [laned, _slow("/unlaned")])

    rows = analytics.execute(
        "SELECT route, response_bytes, dbq_read_ms, db_read_ms, dbq_bulk_ms, db_bulk_ms FROM request_slow ORDER BY route"
    ).fetchall()
    assert rows == [("/laned", 4, 3, 2, 1, 0), ("/unlaned", 0, None, None, None, None)]


def test_bootstrap_adds_lane_columns_to_an_existing_file_as_unknown(tmp_path, monkeypatch):
    import duckdb

    path = tmp_path / "old.duckdb"
    old = duckdb.connect(str(path))
    old.execute(
        "CREATE TABLE request_slow (ts TIMESTAMP NOT NULL, method VARCHAR NOT NULL, route VARCHAR NOT NULL, "
        "status INTEGER NOT NULL, dur_ms DOUBLE NOT NULL, dbq_ms DOUBLE NOT NULL, db_ms DOUBLE NOT NULL)"
    )
    old.execute("CREATE INDEX idx_request_slow_ts ON request_slow (ts)")
    old.execute("INSERT INTO request_slow VALUES (now(), 'GET', '/before', 200, 1, 0, 0)")
    old.close()
    monkeypatch.setattr(analytics_db, "ANALYTICS_DB_PATH", path)
    monkeypatch.delenv("CORSIA_ANALYTICS_DISABLE", raising=False)
    analytics_db._reset_for_tests()
    try:
        row = analytics_db.get_db().execute(
            "SELECT route, response_bytes, dbq_read_ms, db_read_ms, dbq_bulk_ms, db_bulk_ms FROM request_slow"
        ).fetchone()
    finally:
        analytics_db._reset_for_tests()

    assert row == ("/before", None, None, None, None, None)


def test_bootstrap_adds_nullable_byte_columns_and_merges_old_daily_row(tmp_path, monkeypatch):
    import duckdb

    path = tmp_path / "old-timing.duckdb"
    old = duckdb.connect(str(path))
    old.execute(
        "CREATE TABLE request_timing_daily (day DATE NOT NULL, method VARCHAR NOT NULL, route VARCHAR NOT NULL, "
        "status_class VARCHAR NOT NULL, count BIGINT NOT NULL, sum_ms DOUBLE NOT NULL, max_ms DOUBLE NOT NULL, "
        "buckets INTEGER[] NOT NULL, PRIMARY KEY (day, method, route, status_class))"
    )
    old.execute(
        "INSERT INTO request_timing_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (*_KEY, 1, 2, 2, [1]),
    )
    old.close()
    monkeypatch.setattr(analytics_db, "ANALYTICS_DB_PATH", path)
    monkeypatch.delenv("CORSIA_ANALYTICS_DISABLE", raising=False)
    analytics_db._reset_for_tests()
    try:
        analytics = analytics_db.get_db()
        assert analytics.execute(
            "SELECT sum_bytes, max_bytes FROM request_timing_daily WHERE route = '/x'"
        ).fetchone() == (None, None)

        request_timing.write_batch({_KEY: _aggregate(3, response_bytes=500)}, [])
        assert analytics.execute(
            "SELECT count, sum_bytes, max_bytes FROM request_timing_daily WHERE route = '/x'"
        ).fetchone() == (2, 500, 500)
    finally:
        request_timing._reset_for_tests()
        analytics_db._reset_for_tests()


def test_write_batch_failure_rolls_back_the_whole_batch(analytics):
    analytics.execute("DROP TABLE request_slow")

    with pytest.raises(Exception, match="request_slow"):
        request_timing.write_batch({_KEY: _aggregate(10)}, [_slow()])

    assert analytics.execute("SELECT COUNT(*) FROM request_timing_daily").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_record_after_swap_lands_in_next_batch(analytics, monkeypatch):
    written: list[request_timing._Batch] = []
    writing, release = threading.Event(), threading.Event()
    real_write = request_timing.write_batch

    def blocking_write(aggregates, slow):
        writing.set()
        release.wait(5)
        written.append((aggregates, slow))

    _record("/first")
    monkeypatch.setattr(request_timing, "write_batch", blocking_write)
    flush = asyncio.create_task(request_timing.flush_pending())
    await asyncio.to_thread(writing.wait, 5)
    _record("/during-write")
    release.set()
    await flush

    assert [key[2] for key in written[0][0]] == ["/first"]
    monkeypatch.setattr(request_timing, "write_batch", real_write)
    await request_timing.flush_pending()
    routes = analytics.execute("SELECT route FROM request_timing_daily").fetchall()
    assert routes == [("/during-write",)]


@pytest.mark.asyncio
async def test_failed_write_drops_batch_and_counts_failure(analytics, monkeypatch, caplog):
    def failing_write(aggregates, slow):
        raise RuntimeError("disk full")

    _record()  # started_at=0 makes this request slow, so it also queues one slow row
    monkeypatch.setattr(request_timing, "write_batch", failing_write)
    with caplog.at_level(logging.WARNING, logger=request_timing.__name__):
        await request_timing.flush_pending()

    assert request_timing.flush_failures == 1
    assert not request_timing._aggregates and not request_timing._slow_requests
    assert [record.getMessage() for record in caplog.records] == [
        "request timing flush failed; dropped 1 aggregate keys and 1 slow rows: disk full"
    ]


@pytest.mark.asyncio
async def test_disabled_analytics_drops_pending_without_writing(analytics, monkeypatch):
    _record()
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")
    await request_timing.flush_pending()

    assert not request_timing._aggregates
    assert analytics.execute("SELECT COUNT(*) FROM request_timing_daily").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_flush_loop_cancellation_flushes_once(analytics, monkeypatch):
    monkeypatch.setattr(request_timing, "_FLUSH_SECONDS", 3_600)
    task = request_timing.start_flush_loop()
    await asyncio.sleep(0)
    _record()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert analytics.execute("SELECT count FROM request_timing_daily").fetchone()[0] == 1


def test_prune_retention_keeps_boundary_date_and_timestamp(analytics):
    now = datetime(2026, 9, 25, tzinfo=UTC)
    analytics.execute("INSERT INTO request_timing_daily (day, method, route, status_class, count, sum_ms, max_ms, buckets) VALUES ('2026-06-26', 'GET', '/old', '2xx', 1, 1, 1, [1])")
    analytics.execute("INSERT INTO request_timing_daily (day, method, route, status_class, count, sum_ms, max_ms, buckets) VALUES ('2026-06-27', 'GET', '/edge', '2xx', 1, 1, 1, [1])")
    insert_slow = "INSERT INTO request_slow (ts, method, route, status, dur_ms, dbq_ms, db_ms) VALUES (?, 'GET', ?, 200, 1, 0, 0)"
    analytics.execute(insert_slow, (now - timedelta(days=30, microseconds=1), "/old"))
    analytics.execute(insert_slow, (now - timedelta(days=30), "/edge"))

    request_timing.prune_retention(now)

    assert analytics.execute("SELECT route FROM request_timing_daily").fetchall() == [("/edge",)]
    assert analytics.execute("SELECT route FROM request_slow").fetchall() == [("/edge",)]


@pytest.mark.parametrize("days, expected", [(1, ["/today"]), (3, ["/today", "/edge"])])
def test_route_query_window_is_inclusive_of_today(analytics, days, expected):
    today = datetime.now(UTC).date()
    for route, age in (("/today", 0), ("/edge", 2), ("/outside", 3)):
        analytics.execute(
            "INSERT INTO request_timing_daily (day, method, route, status_class, count, sum_ms, max_ms, buckets) VALUES (?, 'GET', ?, '2xx', 1, 1, 1, [1])",
            (today - timedelta(days=age), route),
        )

    rows = request_timing.query_route_timing(analytics.cursor(), days)

    assert sorted(row[2] for row in rows) == sorted(expected)
