"""Startup's own writes stay off the event loop, and its own lines get captured.

The Nov 10 2026 power-cut restart logged eight ``guard_write_off_event_loop``
ERRORs, one per ``initialize_startup`` write scope, and none of its
``startup phase:`` timings reached the conductor log store, because capture only
started after the pre-bind reconcile. Both are proven here on the real
lifespan with real init, reusing the existing lifespan harness.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi import FastAPI

import app_lifecycle
import app_startup_init
from test_app_lifecycle_shutdown import _isolate_lifespan  # noqa: F401 - shared lifespan harness


@pytest.fixture
def guard_reports(monkeypatch):
    """Every loop-guard report emitted while the test runs, dedupe cache cleared."""
    from infra import db_loop_guard

    collected: list[str] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            collected.append(record.getMessage())

    handler = _Handler(logging.WARNING)
    logger = logging.getLogger(db_loop_guard.__name__)
    # The guard logs one report per call site per process, so a test must not
    # inherit another test's already-reported sites.
    monkeypatch.setattr(db_loop_guard, "_loop_write_sites", set())
    logger.addHandler(handler)
    try:
        yield collected
    finally:
        logger.removeHandler(handler)


def _init_recording_loop_and_reports(guard_reports, monkeypatch):
    """Run the real init, noting the thread it ran on and the reports it produced."""
    real_init = app_startup_init.initialize_startup
    on_loop: list[bool] = []
    reports_during_init: list[list[str]] = []

    def init_and_record(logger) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_loop.append(False)
        else:
            on_loop.append(True)
        before = len(guard_reports)
        real_init(logger)
        reports_during_init.append(guard_reports[before:])

    monkeypatch.setattr(app_lifecycle.app_startup_init, "initialize_startup", init_and_record)
    return on_loop, reports_during_init


async def test_startup_init_runs_off_the_event_loop_without_a_guard_report(
    guard_reports, monkeypatch,
):
    """Real init writes on a worker thread, so no outermost write trips the guard."""
    _isolate_lifespan(monkeypatch)
    on_loop, reports_during_init = _init_recording_loop_and_reports(guard_reports, monkeypatch)

    async with app_lifecycle.lifespan(FastAPI()):
        pass

    assert on_loop == [False], "initialize_startup was entered on the event-loop thread"
    assert reports_during_init == [[]], f"init reported loop-thread writes: {reports_during_init}"


async def test_capture_is_on_before_startup_logs_its_database_phase(
    guard_reports, monkeypatch, tmp_path,
):
    """Capture starts first, so init's phase timings reach the conductor log store."""
    from infra.otel_logs import lifecycle
    from infra.otel_logs.store import ReadQuery, read_records

    _isolate_lifespan(monkeypatch)
    store = tmp_path / "otel_logs.db"
    monkeypatch.setenv(lifecycle.TEST_ENABLE_ENV, "1")
    monkeypatch.setattr(lifecycle, "default_store_path", lambda: store)
    lifecycle.stop_corsia_logs()  # this test owns the pipeline its store needs
    state_at_init: list[str] = []
    real_init = app_startup_init.initialize_startup

    def init_and_record(logger) -> None:
        state_at_init.append(lifecycle.capture_status()["state"])
        real_init(logger)

    monkeypatch.setattr(app_lifecycle.app_startup_init, "initialize_startup", init_and_record)
    try:
        async with app_lifecycle.lifespan(FastAPI()):
            pass
    finally:
        lifecycle.stop_corsia_logs()

    assert state_at_init == ["on"], "capture was not already running when init started"
    stored = read_records(store, ReadQuery(
        since_ns=0, until_ns=1 << 62, min_severity=0, limit=5,
        contains="startup phase: database initialization started",
    ))
    assert stored, "the database-initialization phase line never reached the log store"
