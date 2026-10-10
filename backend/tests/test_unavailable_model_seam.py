"""The unavailable-model hold lives at the admission seam and settles like any failure (#2525)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests.unavailable_model_support import (
    HELD,
    hold_head,
    message_row,
    seed_model,
    set_model_available,
)


@pytest.fixture
def no_recovery_task(monkeypatch):
    """The hold's own re-check is covered elsewhere; keep these tests on one path."""
    import infra.unavailable_model_recovery as recovery

    monkeypatch.setattr(recovery, "schedule_unavailable_model_recovery", Mock())


def _thread_row(thread_id: str, model: str) -> dict:
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES (?,?,?,'t0','t0')",
            (thread_id, "user-1", model),
        )
        db.commit()
        return dict(db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone())
    finally:
        db.close()


def _running_turns(thread_id: str) -> int:
    from infra.database import get_db

    db = get_db()
    try:
        return int(db.execute(
            "SELECT COUNT(*) FROM active_turns WHERE thread_id=? AND status='running'",
            (thread_id,),
        ).fetchone()[0])
    finally:
        db.close()


@pytest.mark.asyncio
async def test_send_path_hold_failure_settles_the_claimed_turn(
    isolated_db, monkeypatch, no_recovery_task,
) -> None:
    """A hold that raises must not strand a running Turn on the send path."""
    import infra.input_writes as input_writes
    import infra.send_service as service

    seed_model("omlx@srv/m", available=0)
    thread_row = _thread_row("t", "omlx@srv/m")

    def release_fails(*_args, **_kwargs):
        raise RuntimeError("input boundary claim changed before release: m1")

    monkeypatch.setattr(input_writes, "release_batch_before_write", release_fails)
    monkeypatch.setattr(service, "broadcast_accepted_input", lambda *_a, **_k: None)

    with pytest.raises(service.SendRejected):
        await service.send_thread_input(
            "t", thread_row, "hello", user={"id": "user-1"},
            input_id="m1", native_mode="ws", ws=object(),
        )

    assert _running_turns("t") == 0
    assert message_row("m1")[0] == "failed"


@pytest.mark.asyncio
async def test_boundary_wake_for_an_active_turn_ignores_model_availability(
    isolated_db, monkeypatch,
) -> None:
    """The wake needs only the harness, as thread_execution's boundary wake does."""
    import infra.send_post_admission as post
    import infra.send_service as service

    seed_model("omlx@srv/m", available=0)
    wake = AsyncMock()
    monkeypatch.setattr(service, "request_safe_boundary", wake)
    admission = SimpleNamespace(
        accepted=SimpleNamespace(target_model="omlx@srv/m", thread_id="t"),
    )

    await post.request_boundary_for_admission(admission, None)

    wake.assert_awaited_once_with("t", "native")


@pytest.mark.asyncio
async def test_hold_publishes_the_waiting_row_live(
    isolated_db, monkeypatch, no_recovery_task,
) -> None:
    published = Mock()
    monkeypatch.setattr("infra.state.broadcast_to_thread", published)

    await hold_head(monkeypatch, "t", "omlx@srv/m", "m1")

    payloads = [call.args[1] for call in published.call_args_list if call.args[0] == "t"]
    rows = [
        item for payload in payloads
        for item in (payload.get("messages") or [payload.get("message")]) if item
    ]
    assert [row["id"] for row in rows if row["id"] == "m1"], payloads
    held = [row for row in rows if row["id"] == "m1"][-1]
    assert held["delivery_error"] == "selected_model_unavailable"


@pytest.mark.asyncio
async def test_lifted_hold_stays_visible_while_nothing_can_claim_it(
    isolated_db, monkeypatch, no_recovery_task,
) -> None:
    """A row is released only by the claim that takes it, so a paused thread's
    'Waiting for model' row is never cleared without a broadcast."""
    import infra.thread_execution as execution
    import infra.unavailable_model_recovery as recovery

    await hold_head(monkeypatch, "t", "omlx@srv/m", "m1")
    set_model_available("omlx@srv/m", 1)
    monkeypatch.setattr("plans.public.set_aside_while_paused", lambda *_a, **_k: True)
    drive = AsyncMock()
    monkeypatch.setattr(execution, "drive_claimed_batch", drive)

    await recovery._resume_held_heads()

    drive.assert_not_awaited()
    assert message_row("m1") == HELD


@pytest.mark.asyncio
async def test_hold_lifted_while_an_unrelated_turn_runs_drives_when_it_settles(
    isolated_db, monkeypatch, no_recovery_task,
) -> None:
    """No new trigger: the settling Turn's own successor claim applies the same
    runnable predicate to the held head."""
    from infra.database import get_db
    from infra.thread_driver import finish_turn
    from infra.thread_input_types import ClaimedBatch
    import infra.unavailable_model_recovery as recovery

    await hold_head(monkeypatch, "t", "omlx@srv/m", "m1")
    db = get_db()
    try:
        db.execute(
            "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,"
            "started_at,boot_epoch) SELECT 'other',id,execution_epoch,model,"
            "'running','t2','boot-1' FROM threads WHERE id='t'"
        )
        db.commit()
    finally:
        db.close()
    set_model_available("omlx@srv/m", 1)

    await recovery._resume_held_heads()
    assert message_row("m1") == HELD

    settlement = finish_turn(
        "other", outcome="completed", ended_at="t3",
        successor_turn_id="succ", boot_epoch="boot-1",
    )

    assert isinstance(settlement.successor, ClaimedBatch)
    assert "m1" in [item.message_id for item in settlement.successor.items]
    assert message_row("m1") == ("queued", None, None, "succ")


@pytest.mark.asyncio
async def test_release_publishes_the_cleared_row_live(
    isolated_db, monkeypatch, no_recovery_task,
) -> None:
    import infra.thread_execution as execution
    import infra.unavailable_model_recovery as recovery

    await hold_head(monkeypatch, "t", "omlx@srv/m", "m1")
    set_model_available("omlx@srv/m", 1)
    monkeypatch.setattr(execution, "_execute_claimed_batch", AsyncMock())
    published = Mock()
    monkeypatch.setattr("infra.state.broadcast_to_thread", published)

    await recovery._resume_held_heads()

    rows = [
        item for call in published.call_args_list if call.args[0] == "t"
        for item in (call.args[1].get("messages") or [call.args[1].get("message")])
        if item and item["id"] == "m1"
    ]
    assert rows, published.call_args_list
    assert rows[-1]["delivery_error"] is None


@pytest.mark.asyncio
async def test_hold_reports_the_turn_end_to_plan_liveness(
    isolated_db, monkeypatch, no_recovery_task,
) -> None:
    """#3317: a held worker's Turn ends in error, so its parent hears F1 like any failure."""
    import core.plan_turn_end as plan_turn_end

    reported = AsyncMock()
    monkeypatch.setattr(plan_turn_end, "record_turn_end_logged", reported)

    await hold_head(monkeypatch, "t", "omlx@srv/m", "m1")

    reported.assert_awaited_once()
    assert reported.await_args is not None
    args, kwargs = reported.await_args
    assert args[0] == "t"
    assert kwargs["outcome"] == "error"
    assert kwargs["turn_id"] == "m1-turn"
    assert "omlx@srv/m" in kwargs["error"]
    # A held head resumes through catalog recovery, so no provider-reset wake
    # is armed: that would be a second path resuming the same input.
    assert "clears_at" not in kwargs
