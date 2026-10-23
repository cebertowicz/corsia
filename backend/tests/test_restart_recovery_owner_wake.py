"""A restart that ends a worker's Turn tells the worker's owner in that restart.

Recovery commits the dead Turn's terminal before the port binds, so it never
reached the turn-end hook every live terminal runs. The drive phase records that
hook for each Turn recovery ended, through the same delegation links the live
hook reads, so the owner's wake and the worker's liveness follow.
"""

from __future__ import annotations

import asyncio

import pytest
from loguru import logger

from infra.thread_recovery import RESTART_TURN_ERROR, recover_threads
from tests.child_parent_notice_helpers import begin_worker_turn, delegated, wakes


def _run_turn_on(db, thread_id: str, *, boot_epoch: str | None = None) -> None:
    """Give ``thread_id`` a running Turn from a dead boot, or from ``boot_epoch``."""
    begin_worker_turn(db, "turn-1")
    db.execute(
        "UPDATE active_turns SET thread_id=?, boot_epoch=COALESCE(?,boot_epoch) "
        "WHERE id='turn-1'",
        (thread_id, boot_epoch),
    )
    db.commit()


@pytest.fixture
def seeded(isolated_db):
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "INSERT INTO projects (id,name,created_at,updated_at) "
            "VALUES ('project-1','Project','t','t')"
        )
        for thread_id, spawned_by in (("parent", None), ("worker", "parent"), ("solo", None)):
            db.execute(
                "INSERT INTO threads "
                "(id,user_id,project_id,spawned_by,model,created_at,updated_at) "
                "VALUES (?,'user-1','project-1',?,'native/model','t','t')",
                (thread_id, spawned_by),
            )
        db.commit()
        yield db
    finally:
        db.close()


@pytest.fixture
def delegation(seeded):
    """A parent whose delegated worker has a running Turn from a dead boot."""
    delegated(seeded)
    _run_turn_on(seeded, "worker")
    return seeded


@pytest.fixture
def solo(seeded):
    """An undelegated thread with open plan work and a running Turn from a dead boot."""
    from plans import service

    plan = service.ensure_plan("thread", "solo", db=seeded)
    service.add_node(
        plan.id, kind="task", title="Open work", acceptance_criteria="Done", db=seeded,
    )
    seeded.commit()
    _run_turn_on(seeded, "solo")
    return seeded


@pytest.fixture
def current_epoch(seeded):
    """A delegated worker whose running Turn belongs to this process."""
    from core.turn_core import BOOT_EPOCH

    delegated(seeded)
    _run_turn_on(seeded, "worker", boot_epoch=BOOT_EPOCH)
    return seeded


@pytest.fixture
def driven(monkeypatch):
    """Capture the threads the turn-end hook drives and silence unrelated drives."""
    from automation import restart_startup
    from plans import public

    threads: list[str] = []

    async def drive_threads(thread_ids):
        threads.extend(thread_ids)

    async def replay():
        return None

    monkeypatch.setattr(public, "drive_threads", drive_threads)
    monkeypatch.setattr(restart_startup, "_replay_held_webhooks", replay)
    return threads


async def _restart():
    from automation import restart_startup

    recovered, wake_threads, _rolled_back = await restart_startup.reconcile_startup_continuity(logger)
    await restart_startup.drive_startup_continuity(recovered, wake_threads, logger)
    return recovered


def _turn(db, turn_id: str) -> tuple[str, str | None]:
    row = db.execute("SELECT status,error FROM active_turns WHERE id=?", (turn_id,)).fetchone()
    return str(row["status"]), row["error"]


@pytest.mark.asyncio
async def test_restart_ended_worker_turn_wakes_its_parent(delegation, driven):
    recovered = await _restart()

    assert recovered.ended_turns == (("worker", "turn-1"),)
    assert _turn(delegation, "turn-1") == ("error", RESTART_TURN_ERROR)
    appended = wakes(delegation)
    assert [str(row["id"]).split(":")[1] for row in appended] == ["child_turn_errored"]
    assert RESTART_TURN_ERROR in str(appended[0]["content"])
    assert "parent" in driven

    # The Turn is terminal, so no later pass ends or records it again.
    again = await asyncio.to_thread(
        recover_threads, boot_epoch="boot-3", recovered_at="t9",
        successor_id=lambda _thread_id: "unused",
    )
    assert again.ended_turns == ()
    assert len(wakes(delegation)) == 1


@pytest.mark.asyncio
async def test_restart_ended_turn_without_delegation_updates_liveness_only(solo, driven):
    from automation import plan_liveness

    assert plan_liveness.wake_for("solo", solo) is None

    recovered = await _restart()

    assert recovered.ended_turns == (("solo", "turn-1"),)
    assert plan_liveness.wake_for("solo", solo) is not None
    assert wakes(solo, "parent") == []
    assert "parent" not in driven


@pytest.mark.asyncio
async def test_current_epoch_turn_is_not_recorded(current_epoch, driven):
    recovered = await _restart()

    assert recovered.ended_turns == ()
    assert _turn(current_epoch, "turn-1")[0] == "running"
    assert wakes(current_epoch) == []
    assert driven == []
