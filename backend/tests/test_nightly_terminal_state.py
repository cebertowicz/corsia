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
def run_db(isolated_db):
    """Run every nightly step on the test database.

    ``isolated_db`` repoints the database path itself, so every ``get_db`` binding
    opens it however it was imported. Patching module names instead missed the ones
    bound at import, and the drain and reaper then read the suite's database.
    """
    return isolated_db


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
async def test_retirement_drain_runs_on_the_isolated_database(run_db, monkeypatch):
    """The drain must consume this fixture's outbox, never the suite's database."""
    from jobs import nightly

    handle = "nightly-isolation-probe"
    with sqlite3.connect(run_db) as db:
        db.execute(
            "INSERT INTO managed_process_retirements"
            " (handle, thread_id, head_log_name, tail_log_name, staged_at)"
            " VALUES (?, 'thread-probe', ?, NULL, '2026-10-20T00:00:00Z')",
            (handle, f"{handle}.log"),
        )
    _install_retention(monkeypatch)
    await nightly._run_job_safely()

    with sqlite3.connect(run_db) as db:
        left = db.execute("SELECT COUNT(*) FROM managed_process_retirements").fetchone()[0]
    assert left == 0
    assert _read_run(run_db)["status"] == "ok"


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
async def test_sent_back_recovery_runs_after_the_terminal_record_and_its_failure_is_isolated(run_db, monkeypatch):
    from core import sent_back_corrections
    from infra import worktree_reaper
    from jobs import nightly

    _install_retention(monkeypatch)
    recovery = MagicMock(side_effect=RuntimeError("measurement unavailable"))
    reaper = MagicMock()
    monkeypatch.setattr(sent_back_corrections, "reconcile_sent_back_corrections", recovery)
    monkeypatch.setattr(worktree_reaper, "reap_all_idle", reaper)

    await nightly._run_job_safely()

    recovery.assert_called_once()
    reaper.assert_called_once_with()
    assert _read_run(run_db)["status"] == "ok"
