"""Nightly maintenance scheduler — runs the retention sweep, then the idle
worktree reaper, once per day.

Two triggers:
1. Nightly at 02:00 local time — primary trigger
2. Idle detection — if system idle 4+ hours and the sweep has not run today

The scheduler owns one terminal run record per UTC day. It still lives in the
``fact_extraction_runs`` table: the name is now wrong, but renaming it is a
schema change, and ``initialize_schema`` is create-fresh-or-refuse, so a rename
forces a SCHEMA_VERSION bump and an external converter run against every
existing database. Rename it to ``maintenance_runs`` at the next deliberate
schema version bump, not as a side effect of this removal.
That record is the ONLY throttle on the retention sweep, which is heavyweight
and destructive (shutil.rmtree over working/ dirs, per-session DB writes) and
has no internal rate limit of its own. Removing or bypassing the day gate turns
the sweep into an every-CHECK_INTERVAL recursive delete — see the once-per-day
tests in tests/test_nightly_terminal_state.py before changing this.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from loguru import logger

from infra.constants import (
    NIGHTLY_HOUR,
    NIGHTLY_IDLE_THRESHOLD_HOURS as IDLE_THRESHOLD_HOURS,
    NIGHTLY_CHECK_INTERVAL as CHECK_INTERVAL,
    NIGHTLY_STARTUP_DELAY as STARTUP_DELAY,
)
from infra.database import get_db, write_transaction
from infra.time import utc_now_iso


def _has_run_today() -> bool:
    """Return whether today's recorded disposition blocks another attempt."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db = get_db()
    try:
        row = db.execute(
            "SELECT status FROM fact_extraction_runs WHERE run_date = ?",
            (today,),
        ).fetchone()
    finally:
        db.close()
    return bool(row and row["status"] != "cancelled")


def _claim_today(run_date: str) -> bool:
    """Atomically claim today's maintenance slot. False if it is already taken.

    ``run_date`` is the table's PRIMARY KEY, so ``INSERT OR IGNORE`` is the claim:
    exactly one caller can insert it. This runs BEFORE the sweep, not after, for
    two reasons that both protect a destructive operation:

    * check-and-run is otherwise non-atomic — two schedulers could both observe
      no record and both run ``shutil.rmtree`` over the same directories.
    * if the claim cannot be written, this raises and the sweep does NOT run.
      Recording afterwards meant a failed write left no record, so the next
      interval swept again, and again, every CHECK_INTERVAL.

    A crash mid-sweep leaves the row as ``running``, which blocks further sweeps
    today. That is the safe direction for a recursive delete; tomorrow's date is
    a fresh key.
    """
    with write_transaction(None, scope="claim_nightly_maintenance") as db:
        cursor = db.execute(
            "INSERT OR IGNORE INTO fact_extraction_runs "
            "(run_date, status, result_json, ran_at) VALUES (?, ?, ?, ?)",
            (run_date, "running", "{}", utc_now_iso()),
        )
        return cursor.rowcount == 1


def _record_maintenance_terminal(
    run_date: str,
    status: str,
    *,
    error: str | None,
) -> None:
    """Persist one terminal disposition for today's maintenance attempt.

    This row is what stops the retention sweep running again today. It is
    written whatever the outcome, so a failing sweep does not retry in a loop.
    """
    result: dict[str, str] = {}
    if error is not None:
        result["error"] = error

    with write_transaction(None, scope="record_nightly_maintenance_terminal") as db:
        db.execute(
            "INSERT OR REPLACE INTO fact_extraction_runs "
            "(run_date, status, result_json, ran_at) VALUES (?, ?, ?, ?)",
            (run_date, status, json.dumps(result), utc_now_iso()),
        )


def _get_last_activity_time() -> datetime | None:
    """Return the timestamp of the most recent message in the DB."""
    db = get_db()
    row = db.execute(
        "SELECT MAX(created_at) as last_at FROM messages"
    ).fetchone()
    db.close()
    if not row or not row["last_at"]:
        return None
    try:
        ts = row["last_at"]
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        elif "+" not in ts and not ts.endswith("Z"):
            ts = ts + "+00:00"
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _is_system_idle(threshold_hours: int = IDLE_THRESHOLD_HOURS) -> bool:
    """Return True if no messages have been sent in the last N hours."""
    last_activity = _get_last_activity_time()
    if last_activity is None:
        return True
    idle_duration = datetime.now(timezone.utc) - last_activity
    return idle_duration >= timedelta(hours=threshold_hours)


def _should_run_nightly() -> bool:
    """Return True if it's the nightly window (02:00–03:00 local) and job hasn't run today."""
    if _has_run_today():
        return False
    return datetime.now().hour == NIGHTLY_HOUR


def _should_run_on_idle() -> bool:
    """Return True if system is idle and job hasn't run today."""
    if _has_run_today():
        return False
    return _is_system_idle(threshold_hours=IDLE_THRESHOLD_HOURS)


async def _run_job_safely() -> None:
    """Run the daily retention sweep once, recording a terminal disposition.

    The terminal record is written in ``finally`` so that a failed sweep still
    consumes the day. Without that, a sweep that raises every time would be
    retried every CHECK_INTERVAL for the rest of the day.
    """
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Claim first: a failure here propagates and the sweep does not run.
    if not _claim_today(run_date):
        logger.info("nightly_job: {} already claimed, skipping", run_date)
        return
    logger.info("nightly_job: starting maintenance for {}", run_date)

    status = "error"
    error: str | None = None
    try:
        # Thread lifecycle + retention sweep. Offloaded to a worker thread: the
        # sweep is synchronous and heavyweight (shutil.rmtree, working/ file
        # walks, per-session DB work) and must not block the event loop.
        from automation.managed_process_events import drain_retirements
        from core.thread_lifecycle import run_lifecycle_retention
        from infra.request_timing import prune_retention

        await asyncio.to_thread(run_lifecycle_retention)
        await asyncio.to_thread(prune_retention)
        await asyncio.to_thread(drain_retirements)
        status = "ok"
        logger.info("nightly_job: maintenance complete for {}", run_date)
    except asyncio.CancelledError as exc:
        status = "cancelled"
        error = str(exc) or "cancelled"
        raise
    except Exception as exc:
        status = "error"
        error = str(exc)
        logger.exception("nightly_job: lifecycle retention sweep failed")
    finally:
        try:
            _record_maintenance_terminal(run_date, status, error=error)
        except Exception as rec_err:
            logger.warning(
                "nightly_job: terminal record failed for status={}: {}",
                status,
                rec_err,
            )
    # Follow-up jobs run after the terminal record: their failures cannot change
    # the maintenance disposition already written.
    await _emit_learning_reports_safely()
    await _reap_worktrees_safely()


async def _run_scheduled_update_if_due(now: datetime | None = None) -> str | None:
    """Run the D7 scheduled-tick owner and log an unexpected lifecycle failure."""
    from automation.update_schedule import run_scheduled_tick

    try:
        return await run_scheduled_tick(now)
    except Exception:
        logger.exception("nightly_job: scheduled update tick failed")
        return None


async def _emit_learning_reports_safely() -> None:
    """Run the learning producer on the established once-daily job path."""
    from jobs.learning_report import emit_daily_reports

    try:
        await asyncio.to_thread(emit_daily_reports)
    except Exception:
        # This follow-up must not change the already-recorded maintenance disposition.
        logger.exception("nightly_job: learning report delivery failed")


async def _reap_worktrees_safely() -> None:
    """Run the idle worktree reaper once; log and swallow any failure."""
    try:
        from infra.worktree_reaper import reap_all_idle
        await asyncio.to_thread(reap_all_idle)
    except Exception:
        logger.exception("nightly_job: worktree reaper failed")


async def nightly_job_loop() -> None:
    """Background loop for maintenance only; correction recording is arrival-driven."""
    await asyncio.sleep(STARTUP_DELAY)
    logger.info("nightly_job: loop started (checking every {}s)", CHECK_INTERVAL)
    while True:
        try:
            await _run_scheduled_update_if_due()
            if _should_run_nightly():
                logger.info("nightly_job: nightly trigger fired (02:00 window)")
                await _run_job_safely()
            elif _should_run_on_idle():
                logger.info("nightly_job: idle trigger fired ({}h+ idle)", IDLE_THRESHOLD_HOURS)
                await _run_job_safely()
        except Exception:
            logger.exception("nightly_job: loop error")
        await asyncio.sleep(CHECK_INTERVAL)
