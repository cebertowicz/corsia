"""The read and bulk DB lanes: routing, read-only enforcement, cancellation, shutdown and timing."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading

import pytest

from infra import db_async, request_timing
from infra.database import get_db, shutdown_pool, write_transaction
from infra.db_async import run_bulk, run_in_db, run_read
from infra.db_connection import LaneWriteError

_LANES = {"read": run_read, "bulk": run_bulk}


def _on_lane(job):
    """Run ``job(db)`` on the current thread's pooled connection, returning (thread name, result)."""
    def run():
        db = get_db()
        try:
            return threading.current_thread().name, job(db)
        finally:
            db.close()
    return run


def _seed_thread(thread_id: str = "lane-thread") -> None:
    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id, user_id, title, model, created_at, updated_at) "
            "VALUES (?, 'steve', 'Lane probe', 'm', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')",
            (thread_id,),
        )
        db.commit()
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["read", "bulk"])
async def test_each_lane_runs_on_its_own_named_threads(isolated_db, lane):
    name, value = await _LANES[lane](_on_lane(lambda db: db.execute("SELECT 1").fetchone()[0]))
    default_name, _ = await run_in_db(_on_lane(lambda db: None))

    assert name.startswith(f"db-{lane}_") and value == 1
    assert not default_name.startswith(("db-read", "db-bulk"))


@pytest.mark.asyncio
async def test_read_lane_refuses_every_write(isolated_db):
    _seed_thread()

    def write(sql):
        return _on_lane(lambda db: db.execute(sql))

    for sql in ("UPDATE threads SET title='x' WHERE id='lane-thread'",
                "CREATE TEMP TABLE lane_probe (value TEXT)"):
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            await run_read(write(sql))

    _, title = await run_in_db(_on_lane(
        lambda db: db.execute("SELECT title FROM threads WHERE id='lane-thread'").fetchone()[0]))
    assert title == "Lane probe"


@pytest.mark.asyncio
async def test_bulk_lane_builds_measurement_temp_tables(isolated_db):
    from datetime import datetime, timezone

    from api.measurement_queries import prepare_outcome_periods, query_family
    from core.measurement_scope import resolve_grain

    grain = resolve_grain("weeks", "UTC", now=datetime(2026, 9, 28, 12, tzinfo=timezone.utc))
    params = {"user_id": "steve", "scope_project": "all", "scope_agent": None, "grain": grain}

    def prepare(db):
        prepare_outcome_periods(db, params, roots=True)
        return query_family(db, "outcomes", params)

    _, rows = await run_bulk(_on_lane(prepare))
    assert isinstance(rows, list)


@pytest.mark.asyncio
@pytest.mark.parametrize("sql", [
    "INSERT INTO settings (key, value) VALUES ('lane', 'x')",
    "UPDATE threads SET title='x'",
    "DELETE FROM threads",
    "CREATE TABLE main.lane_probe (value TEXT)",
    "ATTACH DATABASE ':memory:' AS lane_probe",
    "PRAGMA journal_mode=DELETE",
    "PRAGMA user_version=7",
    "PRAGMA query_only=OFF",
])
async def test_bulk_lane_denies_main_writes_attach_and_writing_pragmas(isolated_db, sql):
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        await run_bulk(_on_lane(lambda db: db.execute(sql)))


@pytest.mark.asyncio
async def test_bulk_lane_allows_read_only_pragmas(isolated_db):
    def introspect(db):
        return (db.execute("PRAGMA table_info(threads)").fetchall(),
                db.execute("PRAGMA user_version").fetchone()[0],
                db.execute("PRAGMA database_list").fetchall())

    _, (columns, _version, databases) = await run_bulk(_on_lane(introspect))
    assert columns and databases


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["read", "bulk"])
async def test_write_transaction_refuses_a_lane_connection(isolated_db, lane):
    def open_write():
        with write_transaction(None, scope="lane_probe"):
            pass

    with pytest.raises(LaneWriteError, match=f"{lane} lane"):
        await _LANES[lane](open_write)

    # The refusal took no write lock: the default lane can still write.
    _seed_thread()


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["read", "bulk"])
async def test_lane_cancellation_joins_the_exact_worker(lane):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def blocked() -> None:
        started.set()
        release.wait(timeout=5)
        finished.set()

    task = asyncio.create_task(_LANES[lane](blocked))
    try:
        await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
    finally:
        release.set()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_pool_close_waits_for_jobs_in_every_lane(isolated_db, caplog):
    started = {lane: threading.Event() for lane in ("default", "read", "bulk")}
    release = threading.Event()

    def holding(lane):
        def job():
            db = get_db()
            try:
                db.execute("SELECT 1").fetchone()
                started[lane].set()
                release.wait(timeout=5)
            finally:
                db.close()
            return lane
        return job

    tasks = [asyncio.create_task(run(holding(lane)))
             for lane, run in (("default", run_in_db), ("read", run_read), ("bulk", run_bulk))]
    try:
        for event in started.values():
            await asyncio.wait_for(asyncio.to_thread(event.wait), timeout=1)
        closing = asyncio.create_task(asyncio.to_thread(shutdown_pool))
        while not db_async._pool_closing:
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        assert not closing.done()
        with caplog.at_level(logging.WARNING, logger="infra.db_connection"):
            release.set()
            assert sorted(await asyncio.gather(*tasks)) == ["bulk", "default", "read"]
            await closing
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)

    # The bulk connection's authorizer was lifted, so its checkpoint ran too.
    assert not [r for r in caplog.records if "on shutdown failed" in r.getMessage()]
    # Lanes reopen read-only connections after the pool was replaced.
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        await run_read(_on_lane(lambda db: db.execute("CREATE TEMP TABLE lane_probe (v TEXT)")))


@pytest.mark.asyncio
async def test_request_timing_keeps_lanes_apart():
    timing = request_timing.DbTiming()
    token = request_timing._request_db_timing.set(timing)
    try:
        await run_in_db(lambda: None)
        await run_read(lambda: None)
        await run_bulk(lambda: None)
    finally:
        request_timing._request_db_timing.reset(token)

    assert timing.queue_ms >= timing.read_queue_ms + timing.bulk_queue_ms
    assert timing.execution_ms >= timing.read_execution_ms + timing.bulk_execution_ms
    header = request_timing._server_timing(0.0, timing).decode()
    for name in ("dbq", "db", "dbq_read", "db_read", "dbq_bulk", "db_bulk"):
        assert f"{name};dur=" in header


def test_add_db_timing_attributes_each_lane():
    timing = request_timing.DbTiming()
    token = request_timing._request_db_timing.set(timing)
    try:
        request_timing.add_db_timing(0.001, 0.002)
        request_timing.add_db_timing(0.003, 0.004, "read")
        request_timing.add_db_timing(0.005, 0.006, "bulk")
    finally:
        request_timing._request_db_timing.reset(token)

    assert (timing.queue_ms, timing.execution_ms) == pytest.approx((9, 12))
    assert (timing.read_queue_ms, timing.read_execution_ms) == pytest.approx((3, 4))
    assert (timing.bulk_queue_ms, timing.bulk_execution_ms) == pytest.approx((5, 6))


# --- Migrated routes run, unpatched, on the read lane ------------------------------


@pytest.fixture
def lanes_seen(monkeypatch):
    seen: list[str | None] = []
    real = request_timing.add_db_timing

    def record(queue_seconds, execution_seconds, lane=None):
        seen.append(lane)
        real(queue_seconds, execution_seconds, lane)

    monkeypatch.setattr(request_timing, "add_db_timing", record)
    return seen


@pytest.mark.asyncio
async def test_page_reads_succeed_on_the_read_lane(isolated_db, monkeypatch, lanes_seen):
    from api import projects, thread_messages, threads, vendor_accounts

    user = {"id": "steve", "token_kind": "session"}
    for module in (projects, thread_messages, threads):
        monkeypatch.setattr(module, "authenticate", lambda _token: user)
    _seed_thread()

    assert "threads" in await threads.list_threads("tok")
    await projects.list_projects("tok")
    await thread_messages.get_messages("lane-thread", "tok", limit=20)
    assert "accounts" in await vendor_accounts.list_vendor_accounts()

    assert "read" in lanes_seen


@pytest.mark.asyncio
async def test_plan_and_model_reads_succeed_on_the_read_lane(isolated_db, monkeypatch, lanes_seen):
    from fastapi import HTTPException, Response

    from api import models, plans
    from connections import harness_refresh
    from infra.runtime_truth import read_runtime_truth

    # Listing models schedules a background harness refresh, which writes on the
    # default lane; keep it out so only the routes' own jobs are counted.
    monkeypatch.setattr(harness_refresh, "schedule_stale_refresh", lambda: None)
    user = {"id": "steve", "token_kind": "session"}
    _seed_thread()

    await plans.read_activity_workspace(Response(), None, user)
    await plans.read_operator_workspace(Response(), None, user)
    try:
        await plans.read_operator_thread("lane-thread", Response(), None, user)
    except HTTPException as error:  # a thread with no plan reads as unavailable
        assert error.status_code == 404
    assert await plans.read_operator_thread_states("lane-thread", user) is not None
    assert await models.list_models()
    assert lanes_seen == ["read"] * 6

    _, row = await run_in_db(_on_lane(
        lambda db: dict(db.execute("SELECT * FROM threads WHERE id='lane-thread'").fetchone())))
    lanes_seen.clear()
    assert isinstance(await read_runtime_truth("lane-thread", row), dict)

    assert lanes_seen and set(lanes_seen) == {"read"}


@pytest.mark.asyncio
@pytest.mark.parametrize("handed_off", [True, False])
async def test_conductor_first_run_check_reads_cleanly_on_the_read_lane(
    isolated_db, caplog, handed_off,
):
    """``first_run_pending`` reads True on a database error, so a lane refusal there would be silent."""
    from infra.onboarding_events import first_run_message_id, first_run_pending

    _seed_thread("conductor")
    db = get_db()
    try:
        db.execute(
            "INSERT INTO messages (id, thread_id, role, content, created_at, seq) "
            "VALUES (?, 'conductor', 'user', 'wake', '2026-10-01T00:00:00+00:00', 1)",
            (first_run_message_id("conductor"),),
        )
        if handed_off:
            db.execute(
                "INSERT INTO thread_updates (thread_id, kind, source_kind, source_id, after_seq, created_at) "
                "VALUES ('conductor', 'marked', 'message', 'handoff', 1, '2026-10-01T00:01:00+00:00')"
            )
        db.commit()
    finally:
        db.close()

    with caplog.at_level(logging.WARNING, logger="infra.onboarding_events"):
        _, pending = await run_read(_on_lane(lambda conn: first_run_pending("conductor", db=conn)))

    assert pending is (not handed_off)
    assert not [r for r in caplog.records if "read failed" in r.getMessage()]
