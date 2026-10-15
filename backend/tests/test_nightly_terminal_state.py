"""The nightly scheduler's day gate is the ONLY throttle on retention.

``run_lifecycle_retention`` is heavyweight and destructive — shutil.rmtree over
working/ dirs, full directory walks, per-session DB writes — and has no internal
rate limit. Its once-per-day guarantee comes entirely from the terminal record
these tests exercise. If the gate regresses, the sweep runs every
CHECK_INTERVAL, forever, while the system is idle.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def run_db(tmp_path, monkeypatch):
    """Give the nightly scheduler an isolated run table."""
    path = tmp_path / "nightly-runs.db"
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE fact_extraction_runs (
                   run_date TEXT PRIMARY KEY,
                   status TEXT NOT NULL DEFAULT 'ok',
                   result_json TEXT,
                   ran_at TEXT NOT NULL
               )"""
        )

    def factory():
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        return db

    from jobs import nightly

    monkeypatch.setattr(nightly, "get_db", factory)
    monkeypatch.setattr("infra.database.get_db", factory)
    return path


def _read_run(path):
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return db.execute(
            "SELECT status, result_json FROM fact_extraction_runs WHERE run_date = ?",
            (run_date,),
        ).fetchone()


def _install_retention(monkeypatch, *, error=None):
    from core import thread_lifecycle

    retention = MagicMock(side_effect=error)
    monkeypatch.setattr(thread_lifecycle, "run_lifecycle_retention", retention)
    return retention


@pytest.mark.asyncio
async def test_successful_sweep_records_ok_and_runs_retention(run_db, monkeypatch):
    from jobs import nightly

    retention = _install_retention(monkeypatch)
    await nightly._run_job_safely()

    retention.assert_called_once_with()
    row = _read_run(run_db)
    assert row["status"] == "ok"


@pytest.mark.asyncio
async def test_failed_sweep_still_consumes_the_day(run_db, monkeypatch):
    """A sweep that raises must not be retried every CHECK_INTERVAL."""
    from jobs import nightly

    _install_retention(monkeypatch, error=RuntimeError("disk gone"))
    await nightly._run_job_safely()

    row = _read_run(run_db)
    assert row["status"] == "error"
    assert nightly._has_run_today() is True


@pytest.mark.asyncio
async def test_cancelled_sweep_may_retry_today(run_db, monkeypatch):
    from jobs import nightly

    _install_retention(monkeypatch, error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await nightly._run_job_safely()

    assert _read_run(run_db)["status"] == "cancelled"
    assert nightly._has_run_today() is False


@pytest.mark.asyncio
async def test_retention_runs_exactly_once_per_day_on_the_nightly_path(
    run_db, monkeypatch
):
    """The 02:00 trigger must not fire a second sweep the same day."""
    from jobs import nightly

    retention = _install_retention(monkeypatch)
    monkeypatch.setattr(nightly, "_is_system_idle", lambda **_: False)

    class _TwoAM(datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime(2026, 8, 28, 2, 30, tzinfo=tz)
            return base if tz else base.replace(tzinfo=None)

    monkeypatch.setattr(nightly, "datetime", _TwoAM)

    assert nightly._should_run_nightly() is True
    await nightly._run_job_safely()

    # Same day, same window, second check: the gate must hold.
    assert nightly._should_run_nightly() is False
    retention.assert_called_once_with()


@pytest.mark.asyncio
async def test_retention_runs_exactly_once_per_day_on_the_idle_path(
    run_db, monkeypatch
):
    """The idle trigger must not fire a second sweep the same day.

    This is the dangerous one: idle is continuously true while nobody is using
    the system, so without the gate the sweep repeats every CHECK_INTERVAL.
    """
    from jobs import nightly

    retention = _install_retention(monkeypatch)
    monkeypatch.setattr(nightly, "_is_system_idle", lambda **_: True)

    assert nightly._should_run_on_idle() is True
    await nightly._run_job_safely()

    assert nightly._should_run_on_idle() is False
    for _ in range(5):
        if nightly._should_run_on_idle():
            await nightly._run_job_safely()
    retention.assert_called_once_with()


@pytest.mark.asyncio
async def test_claim_is_taken_before_the_sweep_runs(run_db, monkeypatch):
    """The day must be claimed first, so a concurrent scheduler cannot also sweep."""
    from jobs import nightly

    observed: list[bool] = []

    def _retention():
        # While the sweep is running, today must already be claimed.
        observed.append(nightly._has_run_today())

    monkeypatch.setattr(
        "core.thread_lifecycle.run_lifecycle_retention", _retention
    )
    await nightly._run_job_safely()

    assert observed == [True], "the slot must be claimed before the sweep starts"


@pytest.mark.asyncio
async def test_second_scheduler_does_not_repeat_the_sweep(run_db, monkeypatch):
    """Two schedulers racing the same day: exactly one sweep."""
    from jobs import nightly

    retention = _install_retention(monkeypatch)
    await nightly._run_job_safely()
    await nightly._run_job_safely()

    retention.assert_called_once_with()


@pytest.mark.asyncio
async def test_unclaimable_day_does_not_sweep(run_db, monkeypatch):
    """If the claim cannot be written, the destructive sweep must NOT run.

    Recording the disposition afterwards meant a write failure left no record,
    so the next interval swept again -- every CHECK_INTERVAL, forever.
    """
    from jobs import nightly

    retention = _install_retention(monkeypatch)

    def _fail(_run_date):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(nightly, "_claim_today", _fail)

    with pytest.raises(sqlite3.OperationalError):
        await nightly._run_job_safely()

    retention.assert_not_called()


@pytest.mark.asyncio
async def test_manual_claim_does_not_start_a_second_drain_while_nightly_drain_runs(
    monkeypatch,
):
    from jobs import corrections_recording, nightly

    started = asyncio.Event()
    cancelled = asyncio.Event()
    claims = 0
    starts = 0
    nightly_checks = iter([True, False])
    real_sleep = asyncio.sleep
    sleeps = 0

    async def drain():
        nonlocal starts
        starts += 1
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def controlled_sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        await real_sleep(0)
        if sleeps == 2:
            await asyncio.wait_for(started.wait(), timeout=0.5)
        if sleeps == 3:
            raise asyncio.CancelledError()

    def claim_manual():
        nonlocal claims
        claims += 1
        return True

    async def no_scheduled_update():
        return None

    async def no_maintenance():
        return None

    monkeypatch.setattr(nightly, "STARTUP_DELAY", 0)
    monkeypatch.setattr(nightly, "_run_scheduled_update_if_due", no_scheduled_update)
    monkeypatch.setattr(nightly, "_should_run_nightly", lambda: next(nightly_checks))
    monkeypatch.setattr(nightly, "_should_run_on_idle", lambda: False)
    monkeypatch.setattr(nightly, "_run_job_safely", no_maintenance)
    monkeypatch.setattr(nightly, "_run_corrections_recording_safely", drain)
    monkeypatch.setattr(
        corrections_recording, "claim_manual_corrections_recording", claim_manual
    )
    monkeypatch.setattr(asyncio, "sleep", controlled_sleep)

    with pytest.raises(asyncio.CancelledError):
        await nightly.nightly_job_loop()

    assert starts == 1
    assert claims == 0
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_manual_corrections_drain_keeps_scheduler_triggers_live_and_cancels_cleanly(monkeypatch):
    from jobs import corrections_recording, nightly

    manual_started = asyncio.Event()
    manual_cancelled = asyncio.Event()
    maintenance_calls: list[bool] = []
    starts = 0
    nightly_checks = iter([False, False, True])
    claims = iter([True, True])
    real_sleep = asyncio.sleep
    sleeps = 0

    async def drain():
        nonlocal starts
        starts += 1
        manual_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            manual_cancelled.set()
            raise

    async def run_maintenance(*, include_corrections=False):
        maintenance_calls.append(include_corrections)

    async def controlled_sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        await real_sleep(0)
        if sleeps == 2:
            await asyncio.wait_for(manual_started.wait(), timeout=0.5)
        if sleeps == 4:
            raise asyncio.CancelledError()

    monkeypatch.setattr(nightly, "STARTUP_DELAY", 0)
    monkeypatch.setattr(nightly, "_should_run_nightly", lambda: next(nightly_checks))
    monkeypatch.setattr(nightly, "_should_run_on_idle", lambda: False)
    monkeypatch.setattr(nightly, "_run_job_safely", run_maintenance)
    monkeypatch.setattr(nightly, "_run_corrections_recording_safely", drain)
    monkeypatch.setattr(corrections_recording, "claim_manual_corrections_recording", lambda: next(claims))
    monkeypatch.setattr(asyncio, "sleep", controlled_sleep)

    with pytest.raises(asyncio.CancelledError):
        await nightly.nightly_job_loop()

    assert starts == 1
    assert maintenance_calls == [False]
    assert manual_cancelled.is_set()
