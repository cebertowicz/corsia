"""The continuation loop sleeps to the persisted batch deadline, not a fixed interval."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from automation.continuation import loop
from doc_feedback_helpers import seed_document, seed_thread
from doceditor.feedback_batches import record_edit
import infra.sweep_wake as sweep_wake
from infra.database import get_db, write_transaction

pytestmark = pytest.mark.asyncio


async def _run_loop_until(sweeps: list[str], count: int, timeout: float) -> None:
    task = asyncio.create_task(loop.continuation_sweep_loop())
    try:
        async with asyncio.timeout(timeout):
            while len(sweeps) < count:
                await asyncio.sleep(0.05)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_loop_sweeps_again_when_an_open_batch_comes_due_and_when_one_is_written(isolated_db, monkeypatch):
    sweeps: list[str] = []

    async def fake_sweep(now, **_kwargs):
        sweeps.append(now)

    monkeypatch.setattr(loop, "sweep", fake_sweep)
    monkeypatch.setattr(loop.restart_jitter, "RESTART_JITTER_SECONDS", 0)
    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact_id = seed_document(db, "owner")
        with write_transaction(db, scope="test_clock") as connection:
            record_edit(
                connection, artifact_id=artifact_id, recipient_id="owner", author_kind="agent", author_id="author",
                actor_display="Author", path=Path("/workspace/shared/x.md"),
                before=b"a\n", after=b"b\n",
            )
        soon = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        db.execute("UPDATE doc_feedback_batches SET closes_at=? WHERE state='open'", (soon,))
        db.commit()
    finally:
        db.close()

    started = asyncio.get_running_loop().time()
    # Without the persisted clock the second sweep would wait the 60s interval.
    await _run_loop_until(sweeps, 2, timeout=5)
    assert asyncio.get_running_loop().time() - started < 4


async def test_sweep_soon_wakes_a_sleeping_loop(isolated_db, monkeypatch):
    sweeps: list[str] = []

    async def fake_sweep(now, **_kwargs):
        sweeps.append(now)

    monkeypatch.setattr(loop, "sweep", fake_sweep)
    monkeypatch.setattr(loop.restart_jitter, "RESTART_JITTER_SECONDS", 0)
    task = asyncio.create_task(loop.continuation_sweep_loop())
    try:
        async with asyncio.timeout(3):
            while not sweeps:
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.2)
            sweep_wake.sweep_soon()
            while len(sweeps) < 2:
                await asyncio.sleep(0.05)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_a_failing_deadline_read_does_not_end_the_loop(isolated_db, monkeypatch):
    sweeps: list[str] = []

    async def fake_sweep(now, **_kwargs):
        sweeps.append(now)

    def broken(_now):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(loop, "sweep", fake_sweep)
    monkeypatch.setattr(loop, "_seconds_to_next_feedback_deadline", broken)
    monkeypatch.setattr(loop.restart_jitter, "RESTART_JITTER_SECONDS", 0)
    task = asyncio.create_task(loop.continuation_sweep_loop())
    try:
        async with asyncio.timeout(3):
            while not sweeps:
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.2)
            sweep_wake.sweep_soon()
            while len(sweeps) < 2:
                await asyncio.sleep(0.05)
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_an_overdue_open_batch_does_not_hide_the_next_deadline(isolated_db):
    from automation.continuation.sources.feedback_batches import FeedbackBatchSource

    db = get_db()
    try:
        seed_thread(db, "owner")
        for artifact_id in (seed_document(db, "owner"), seed_document(db, "owner")):
            with write_transaction(db, scope="test_clock") as connection:
                record_edit(
                    connection, artifact_id=artifact_id, recipient_id="owner", author_kind="agent", author_id="author",
                    actor_display="Author", path=Path("/workspace/shared/x.md"), before=b"a\n", after=b"b\n",
                )
        now = datetime.now(timezone.utc)
        rows = [row["id"] for row in db.execute("SELECT id FROM doc_feedback_batches WHERE state='open'").fetchall()]
        db.execute("UPDATE doc_feedback_batches SET closes_at=? WHERE id=?", ((now - timedelta(seconds=30)).isoformat(), rows[0]))
        if len(rows) > 1:
            db.execute("UPDATE doc_feedback_batches SET closes_at=? WHERE id=?", ((now + timedelta(seconds=7)).isoformat(), rows[1]))
        db.commit()
        until = FeedbackBatchSource().seconds_until_next_deadline(now, db=db)
    finally:
        db.close()

    assert len(rows) > 1
    assert until is not None and 0 < until <= 7
