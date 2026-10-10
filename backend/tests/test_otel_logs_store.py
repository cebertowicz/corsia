"""The capped log store, its one writer, and R2 lifecycle gating."""

from __future__ import annotations

import logging
import time

import pytest
from loguru import logger

from infra.otel_logs import lifecycle
from infra.otel_logs.capture import CapturedRecord
from infra.otel_logs.store import (
    LogStore,
    ReadQuery,
    StoreUnavailable,
    read_records,
)

NS = 1_000_000_000


def _record(n: int, body_bytes: int = 64, level: int = 13) -> CapturedRecord:
    return CapturedRecord(
        ts_ns=n, severity_number=level, severity_text="WARNING", logger="pkg.mod",
        body=f"record {n} " + "x" * body_bytes, exception=None,
        function="f", file_path="backend/pkg/mod.py", line=n,
    )


def _all(path, **filters) -> list[dict]:
    query = ReadQuery(since_ns=0, until_ns=1 << 62, min_severity=0, limit=100_000, **filters)
    return read_records(path, query)


@pytest.fixture
def store(tmp_path):
    opened = LogStore(tmp_path / "otel_logs.db", max_bytes=2 * 1024 * 1024, disk_floor_bytes=0)
    assert opened.open()
    yield opened
    opened.close()


def test_size_cap_prunes_oldest_and_bounds_the_footprint(store):
    now = time.time_ns()
    peak = 0
    for batch in range(200):
        records = [_record(batch * 24 + i, body_bytes=2_000) for i in range(24)]
        store.insert(records, now)
        peak = max(peak, store.footprint())
    assert peak <= store.max_bytes
    assert store.counters.pruned_size > 0
    assert store.counters.dropped_store_cap == 0
    rows = _all(store.path)
    newest = max(r["ts_ns"] for r in rows)
    assert newest == 200 * 24 - 1
    assert min(r["ts_ns"] for r in rows) > 0  # the oldest were pruned


def test_age_prune_deletes_only_expired_rows(store):
    now = time.time_ns()
    store.insert([_record(1)], now - (store.retention_seconds + 60) * NS)
    store.insert([_record(2)], now)
    store.prune(now)
    assert [r["ts_ns"] for r in _all(store.path)] == [2]
    assert store.counters.pruned_age == 1


def test_disk_floor_drops_and_counts(tmp_path):
    floored = LogStore(tmp_path / "otel_logs.db", disk_floor_bytes=1 << 62)
    assert floored.open()
    try:
        assert floored.insert([_record(1)], time.time_ns()) == 0
        assert floored.counters.dropped_disk_floor == 1
    finally:
        floored.close()


def test_one_writer_and_schema_mismatch_recreates(tmp_path):
    path = tmp_path / "otel_logs.db"
    first = LogStore(path, disk_floor_bytes=0)
    assert first.open()
    assert not LogStore(path).open()
    first.insert([_record(5)], time.time_ns())
    first._db.execute("UPDATE store_meta SET value='0' WHERE key='schema_version'")
    first.close()

    again = LogStore(path, disk_floor_bytes=0)
    assert again.open()
    try:
        assert _all(path) == []
    finally:
        again.close()


def test_read_filters_newest_first(store):
    now = time.time_ns()
    store.insert(
        [
            _record(1, level=9),
            CapturedRecord(2, 17, "ERROR", "core.turn", "Turn Failed", "Traceback\nKeyError: 'x'", None, None, None),
            CapturedRecord(3, 13, "WARNING", "core.turnover", "other", None, None, None, None),
            CapturedRecord(4, 13, "WARNING", "infra.database", "database is locked", None, None, None, None),
        ],
        now,
    )
    assert [r["ts_ns"] for r in _all(store.path)] == [4, 3, 2, 1]
    assert [r["ts_ns"] for r in _all(store.path, logger_prefix="core.turn")] == [3, 2]
    assert [r["ts_ns"] for r in _all(store.path, contains="KEYERROR")] == [2]
    assert [r["ts_ns"] for r in _all(store.path, contains="locked")] == [4]
    high = ReadQuery(since_ns=0, until_ns=1 << 62, min_severity=17, limit=10)
    assert [r["ts_ns"] for r in read_records(store.path, high)] == [2]
    window = ReadQuery(since_ns=2, until_ns=4, min_severity=0, limit=1)
    assert [r["ts_ns"] for r in read_records(store.path, window)] == [3, 2]  # limit + 1


def test_read_without_store_is_unavailable(tmp_path):
    with pytest.raises(StoreUnavailable):
        _all(tmp_path / "missing.db")


def test_r2_capture_is_off_for_test_instances_and_on_otherwise():
    assert lifecycle.capture_enabled({}) is True
    assert lifecycle.capture_enabled({"CORSIA_TEST_MODE": "0"}) is True
    assert lifecycle.capture_enabled({"CORSIA_TEST_MODE": "1"}) is False
    assert lifecycle.capture_enabled(
        {"CORSIA_TEST_MODE": "1", lifecycle.TEST_ENABLE_ENV: "1"}
    ) is True


@pytest.fixture
def fresh_lifecycle(monkeypatch):
    monkeypatch.setattr(lifecycle, "_pipeline", None)
    monkeypatch.setattr(lifecycle, "_state", "not_started")
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    yield
    lifecycle.stop_corsia_logs()


def test_r2_pytest_instance_starts_nothing(fresh_lifecycle, tmp_path, monkeypatch):
    monkeypatch.setenv("CORSIA_TEST_MODE", "1")
    monkeypatch.delenv(lifecycle.TEST_ENABLE_ENV, raising=False)
    handlers = set(logger._core.handlers)
    root_before = list(logging.getLogger().handlers)
    assert lifecycle.start_corsia_logs(tmp_path / "otel_logs.db") == "off_test_instance"
    assert set(logger._core.handlers) == handlers
    assert logging.getLogger().handlers == root_before
    assert not (tmp_path / "otel_logs.db").exists()


def test_pipeline_writes_through_and_detaches_on_stop(fresh_lifecycle, tmp_path, monkeypatch):
    monkeypatch.setenv(lifecycle.TEST_ENABLE_ENV, "1")
    path = tmp_path / "otel_logs.db"
    loguru_before = set(logger._core.handlers)
    root_before = list(logging.getLogger().handlers)
    assert lifecycle.start_corsia_logs(path, disk_floor_bytes=0) == "on"
    assert lifecycle.start_corsia_logs(path) == "on"  # idempotent
    logger.warning("through the writer")
    logging.getLogger("pkg").error("stdlib through the writer")
    lifecycle.stop_corsia_logs()
    assert set(logger._core.handlers) == loguru_before
    assert logging.getLogger().handlers == root_before
    bodies = {r["body"] for r in _all(path)}
    assert {"through the writer", "stdlib through the writer"} <= bodies
    assert lifecycle.capture_status()["state"] == "stopped"


def test_second_process_writer_leaves_capture_off(fresh_lifecycle, tmp_path, monkeypatch):
    monkeypatch.setenv(lifecycle.TEST_ENABLE_ENV, "1")
    path = tmp_path / "otel_logs.db"
    holder = LogStore(path)
    assert holder.open()
    try:
        handlers = set(logger._core.handlers)
        assert lifecycle.start_corsia_logs(path) == "off_writer_held"
        assert set(logger._core.handlers) == handlers
    finally:
        holder.close()


def test_store_file_is_separate_from_the_product_database():
    from infra import config

    assert config.OTEL_LOGS_DB_PATH.name == "otel_logs.db"
    assert config.OTEL_LOGS_DB_PATH != config.DB_PATH
    assert config.OTEL_LOGS_DB_PATH.parent == config.STATE_ROOT
