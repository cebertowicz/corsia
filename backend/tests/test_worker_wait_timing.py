"""Worker wait telemetry is independent from HTTP timing."""
import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest

from infra import analytics_db, worker_wait_timing as timing


@pytest.fixture
def analytics(tmp_path, monkeypatch):
    monkeypatch.setattr(analytics_db, "ANALYTICS_DB_PATH", tmp_path / "analytics.duckdb")
    monkeypatch.delenv("CORSIA_ANALYTICS_DISABLE", raising=False)
    analytics_db._reset_for_tests()
    yield analytics_db.get_db()
    analytics_db._reset_for_tests()


@pytest.mark.asyncio
async def test_persistence_merge_retention_and_no_slow_rows(analytics):
    old = (datetime.now(UTC).date() - timedelta(days=timing.RETENTION_DAYS + 1)).isoformat()
    edge = (datetime.now(UTC).date() - timedelta(days=timing.RETENTION_DAYS)).isoformat()
    for day in (old, edge):
        analytics.execute("INSERT INTO worker_wait_daily VALUES (?, 'failed', 1, 1, 1, [1])", (day,))
    timing.record_worker_wait(.001, "assigned")
    await timing.flush_pending()
    timing.record_worker_wait(.004, "assigned")
    timing.record_worker_wait(.02, "cancelled")
    await timing.shutdown()
    rows = analytics.execute("SELECT outcome, count, sum_ms, max_ms, buckets FROM worker_wait_daily ORDER BY day, outcome").fetchall()
    assert rows[0][:4] == ("failed", 1, 1, 1)
    assert rows[1][:4] == ("assigned", 2, 5, 4)
    assert sum(rows[1][4]) == 2
    assert rows[2][:4] == ("cancelled", 1, 20, 20)
    assert analytics.execute("SELECT count(*) FROM request_slow").fetchone()[0] == 0
    assert "worker_wait_ms" not in [r[1] for r in analytics.execute("PRAGMA table_info('request_slow')").fetchall()]


@pytest.mark.asyncio
async def test_disabled_never_starts_and_drops_pending(analytics, monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")
    timing.record_worker_wait(.1, "assigned")
    assert timing._task is None and not timing._pending
    monkeypatch.delenv("CORSIA_ANALYTICS_DISABLE")
    timing.record_worker_wait(.1, "failed")
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")
    await timing.shutdown()
    assert analytics.execute("SELECT count(*) FROM worker_wait_daily").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_write_is_off_loop_and_shutdown_waits_for_it(analytics, monkeypatch):
    started, release = threading.Event(), threading.Event()
    original = timing._write_batch
    def blocking(batch):
        started.set()
        assert release.wait(5)
        original(batch)
    monkeypatch.setattr(timing, "_write_batch", blocking)
    timing.record_worker_wait(.1, "timed_out")
    flushing = asyncio.create_task(timing.flush_pending())
    assert await asyncio.to_thread(started.wait, 5)
    timing.record_worker_wait(.2, "failed")
    closing = asyncio.create_task(timing.shutdown())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await flushing
    await closing
    assert analytics.execute("SELECT count(*) FROM worker_wait_daily").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_failed_flush_is_bounded_and_reported(analytics, monkeypatch, caplog):
    def fail(batch):
        raise RuntimeError("write failed")
    monkeypatch.setattr(timing, "_write_batch", fail)
    before = timing.flush_failures
    timing.record_worker_wait(.1, "failed")
    await timing.shutdown()
    assert timing.flush_failures == before + 1
    assert not timing._pending
    assert "write failed" in caplog.text


@pytest.mark.asyncio
async def test_invalid_values_do_not_start_task(monkeypatch):
    monkeypatch.delenv("CORSIA_ANALYTICS_DISABLE", raising=False)
    for duration, outcome in [(1, "other"), (float('nan'), "failed"), (float('inf'), "failed")]:
        with pytest.raises(ValueError):
            timing.record_worker_wait(duration, outcome)
    assert timing._task is None
