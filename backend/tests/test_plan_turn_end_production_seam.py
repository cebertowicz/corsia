"""The core terminal seam returns review work immediately after a real Turn ends."""

from __future__ import annotations

import logging
import sqlite3
import uuid
from unittest.mock import MagicMock

import pytest

from core.plan_turn_end import record_turn_end_logged
from infra.database import get_db
from infra.thread_driver import finish_turn
from plans import mutations, service
from tests.plan_review_helpers import submit_review_evidence

STAMP = "2026-09-21T20:00:00+00:00"


@pytest.mark.parametrize(
    ("preparation_fails", "lock_refused", "external"),
    [(False, False, False), (True, False, False), (False, True, False), (False, True, True)],
)
@pytest.mark.asyncio
async def test_record_turn_end_logged_harvests_after_the_worker_turn_commits(
    monkeypatch,
    caplog,
    preparation_fails,
    lock_refused,
    external,
):
    """A post-commit notification failure or a refused lock cannot strand the return."""

    db = get_db()
    suffix = uuid.uuid4().hex
    project_id = f"turn-end-project-{suffix}"
    parent_id = f"turn-end-parent-{suffix}"
    worker_id = f"turn-end-worker-{suffix}"
    turn_id = f"turn-end-{suffix}"
    db.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES (?,?,?,?)",
        (project_id, f"Turn-end production seam {suffix}", STAMP, STAMP),
    )
    for thread_id, spawned_by in ((parent_id, None), (worker_id, parent_id)):
        db.execute(
            """INSERT INTO threads
               (id,user_id,project_id,spawned_by,model,created_at,updated_at)
               VALUES (?,'user-1',?,?,'test/model',?,?)""",
            (thread_id, project_id, spawned_by, STAMP, STAMP),
        )
    parent_plan = service.ensure_plan("thread", parent_id, db=db)
    delegated = service.add_node(
        parent_plan.id,
        kind="task",
        title="Review the returned work",
        acceptance_criteria="The worker result is reviewable",
        spawn_kind="claude-code",
        db=db,
    )
    service.transition_node(delegated.id, "active", db=db)
    worker_plan = service.ensure_plan("thread", worker_id, db=db)
    mutations.attach_spawned_thread(delegated.id, worker_id, db=db)
    leaf = service.add_node(
        worker_plan.id,
        kind="task",
        title="Deliver the result",
        acceptance_criteria="The result is ready for review",
        db=db,
    )
    service.transition_node(leaf.id, "active", actor_id=worker_id, db=db)
    submit_review_evidence(leaf.id, db=db, evidence="Proof", actor_id=worker_id)
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES (?,?,0,'test/model','running',?,'boot-1')""",
        (turn_id, worker_id, STAMP),
    )
    db.commit()
    finish_turn(
        turn_id,
        outcome="completed",
        ended_at=STAMP,
        successor_turn_id=f"{turn_id}-next",
        boot_epoch="boot-1",
        db=db,
    )
    assert (
        db.execute(
            "SELECT 1 FROM active_turns WHERE thread_id=? AND status='running'",
            (worker_id,),
        ).fetchone()
        is None
    )

    delivered: list[dict] = []
    driven: list[str] = []
    plan_log = MagicMock()
    monkeypatch.setattr("core.plan_turn_end.logger.log", plan_log)
    monkeypatch.setattr(
        "plans.notifications.state.broadcast_to_user",
        lambda _user_id, message: delivered.append(message),
    )
    if preparation_fails:
        caplog.set_level(logging.WARNING, logger="plans.notifications")

        def fail_notification_connection():
            raise RuntimeError("notification database unavailable")

        monkeypatch.setattr("plans.notifications.get_db", fail_notification_connection)

    async def record_drive(thread_id: str) -> None:
        driven.append(thread_id)

    monkeypatch.setattr("infra.thread_execution.drive_thread", record_drive)
    if lock_refused:
        from automation import plan_liveness_terminal

        real_complete = plan_liveness_terminal.on_thread_turn_complete
        # More refusals than any finite ladder that gives up would absorb.
        refusals = [sqlite3.OperationalError("database is locked")] * 4

        def refuse_while_locked(*args, **kwargs):
            if refusals:
                raise refusals.pop()
            return real_complete(*args, **kwargs)

        import infra.lock_retry

        monkeypatch.setattr(
            plan_liveness_terminal, "on_thread_turn_complete", refuse_while_locked
        )
        monkeypatch.setattr(infra.lock_retry, "FIRST_DELAY_S", 0.0)

    if external:
        # The Claude Code and Codex harnesses write the same terminal directly.
        from automation.turn_end_effects import record_external_turn_end

        effects = await record_external_turn_end(worker_id)
        assert list(effects.drives) == [parent_id]
    else:
        await record_turn_end_logged(
            worker_id,
            "terminal write failed",
            "parent drive failed",
        )

    assert service.get_node(delegated.id, db=db).state == "review_requested"
    assert (
        db.execute(
            "SELECT COUNT(*) FROM plan_node_events "
            "WHERE node_id=? AND event='child_plan_completed'",
            (delegated.id,),
        ).fetchone()[0]
        == 1
    )
    if external:
        return
    if preparation_fails:
        assert delivered == []
        assert any(
            "Post-commit plans_changed preparation failed" in message
            for message in caplog.messages
        )
    else:
        assert len(delivered) == 1
        assert delivered[0]["type"] == "plans_changed"
    assert driven == [parent_id]
    plan_log.assert_not_called()


@pytest.mark.asyncio
async def test_a_refusal_after_the_write_committed_is_not_retried(monkeypatch):
    """Only a refused write is retried: one refused after it committed would repeat it."""
    import automation.turn_end_effects as turn_end_effects

    writes: list[str] = []
    refusals = [sqlite3.OperationalError("database is locked")]

    def complete(thread_id, *, effects, **_outcome):
        writes.append(thread_id)

    def refuse_prepare(pending):
        if refusals:
            raise refusals.pop()
        return turn_end_effects.TurnEndEffects((), ())

    monkeypatch.setattr(turn_end_effects, "_prepare_committed_effects", refuse_prepare)

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        await turn_end_effects.record_turn_end(complete, "committed-once")
    assert writes == ["committed-once"]
