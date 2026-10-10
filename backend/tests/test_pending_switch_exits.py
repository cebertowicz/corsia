"""Pending selection has a natural owner, one fresh proof, or an explicit exit."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_unwritten_switch_suffix import _finish, _request
from test_checkpoint_unavailable_release import _hold, blocked_input  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("always_races", [False, True])
async def test_idle_sequencing_race_gets_one_fresh_preparation(blocked_input, monkeypatch, always_races):
    from thread import model_pending
    from thread.model_state import ModelSwitchSequencingRequired
    from infra.model_switches import read_switch

    _hold(blocked_input)
    _finish(blocked_input)
    _request(blocked_input)
    attempts = []

    async def commit(request):
        attempts.append(request.operation_id)
        if always_races or len(attempts) == 1:
            raise ModelSwitchSequencingRequired("history changed during preparation")
        return SimpleNamespace(status="committed")

    monkeypatch.setattr(model_pending, "_commit_fitting_switch", commit)
    monkeypatch.setattr(model_pending, "_drive_destination", AsyncMock())
    wake = AsyncMock()
    monkeypatch.setattr("infra.thread_execution.drive_thread", wake)
    result = await model_pending.drive_pending_model_switch("held-thread")
    assert len(attempts) == 2
    assert result.status == ("failed" if always_races else "committed")
    if always_races:
        wake.assert_awaited_once_with("held-thread")  # R16: the failure wakes the source.
        assert read_switch("held-thread") is None
        assert blocked_input.execute("SELECT switch_error FROM threads WHERE id='held-thread'").fetchone()[0]


@pytest.mark.asyncio
async def test_legacy_held_switch_is_reconciled_on_its_existing_thread_wake(blocked_input, monkeypatch):
    from infra import thread_execution
    from thread import model_pending

    assert _request(blocked_input).waiting_for_source
    _hold(blocked_input)
    # A prior-process terminal row with the old pending boundary is durable input,
    # not a reason to depend on another user message to run its missing transition.
    blocked_input.execute("UPDATE active_turns SET status='error' WHERE id='held-turn'")
    blocked_input.commit()
    committed = []

    async def commit(request):
        assert not request.waiting_for_source
        assert request.checkpoint_choice == "target"
        committed.append(request.operation_id)
        return SimpleNamespace(status="committed")

    monkeypatch.setattr(model_pending, "_commit_fitting_switch", commit)
    monkeypatch.setattr(model_pending, "_drive_destination", AsyncMock())
    monkeypatch.setattr(thread_execution, "drive_idle_thread", AsyncMock())
    await thread_execution.drive_thread("held-thread")
    assert committed == ["switch-held"]
    assert blocked_input.execute("SELECT target_execution_epoch FROM messages WHERE id='held-input'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_startup_includes_pending_switch_without_a_fifo_claim(isolated_db, monkeypatch):
    from automation import restart_startup
    from infra.database import get_db
    from infra.model_switches import request_switch
    from loguru import logger

    db = get_db()
    db.execute("INSERT INTO threads (id,user_id,model,created_at,updated_at) VALUES ('idle-switch','user-1','oMLX/qwen','t0','t0')")
    db.commit()
    request_switch("idle-switch", "native/target", operation_id="idle-operation", requested_at="t1", db=db)
    db.close()
    recovered, wakes = await restart_startup.reconcile_startup_continuity(logger)
    assert not recovered.batches
    assert wakes.count("idle-switch") == 1
    drive = AsyncMock()
    monkeypatch.setattr("infra.thread_execution.drive_thread", drive)
    monkeypatch.setattr(restart_startup, "_replay_held_webhooks", AsyncMock())
    await restart_startup.drive_startup_continuity(recovered, wakes, logger)
    drive.assert_awaited_once_with("idle-switch")


@pytest.mark.asyncio
async def test_stale_failure_does_not_fail_replacement_selection(blocked_input, monkeypatch):
    from thread import model_pending
    from thread.model_state import ModelSwitchConflict
    from infra.model_switches import read_switch

    _hold(blocked_input)
    _finish(blocked_input)
    _request(blocked_input)

    async def commit(_old):
        _request(blocked_input, "native/new-target", "replacement")
        raise ModelSwitchConflict("old selection lost its proof")

    monkeypatch.setattr(model_pending, "_commit_fitting_switch", commit)
    result = await model_pending.drive_pending_model_switch("held-thread")
    assert result.operation_id == "replacement"
    assert read_switch("held-thread").operation_id == "replacement"
    assert result.status == "preparing"


def _durable_thread_row():
    """Read the switch row on its own connection, as a losing driver would."""
    from infra.database import get_db

    db = get_db()
    try:
        return db.execute(
            "SELECT * FROM threads WHERE id='held-thread'",
        ).fetchone()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_concurrent_drivers_keep_the_first_failure_cause(blocked_input, monkeypatch):
    """Two drivers race one request; the loser must not erase the real cause."""
    from thread import model_pending
    from thread.model_commit import _require_pending_boundary
    from thread.model_state import ModelSwitchPreparationError

    _hold(blocked_input)
    _finish(blocked_input)
    _request(blocked_input)
    both_prepared = asyncio.Event()
    first_settled = asyncio.Event()
    attempts = []

    async def commit(request):
        attempts.append(request.operation_id)
        if len(attempts) == 1:
            await both_prepared.wait()  # Both drivers hold a preparation.
            raise ModelSwitchPreparationError("cold fold input does not fit")
        both_prepared.set()
        await first_settled.wait()  # The winner's failure is now durable.
        # The loser revalidates the real predicate against the row it now reads.
        _require_pending_boundary(
            _durable_thread_row(),
            SimpleNamespace(destination_model=request.destination_model),
        )
        raise AssertionError("a stale preparation must not reach its commit")

    monkeypatch.setattr(model_pending, "_commit_fitting_switch", commit)
    monkeypatch.setattr(model_pending, "_drive_destination", AsyncMock())
    monkeypatch.setattr("infra.thread_execution.drive_thread", AsyncMock())

    async def driver():
        result = await model_pending.drive_pending_model_switch("held-thread")
        first_settled.set()
        return result

    results = await asyncio.gather(driver(), driver())

    assert len(attempts) == 2
    assert blocked_input.execute(
        "SELECT switch_status,switch_error FROM threads WHERE id='held-thread'",
    ).fetchone()[0:2] == ("failed", "cold fold input does not fit")
    assert sorted(
        "none" if item is None else item.status for item in results
    ) == ["failed", "none"]  # The loser reports durable state; it writes none.


@pytest.mark.asyncio
async def test_moved_pending_boundary_commits_the_current_request(blocked_input, monkeypatch):
    """A boundary that moved is re-read once, not failed on a stale snapshot."""
    from thread import model_pending
    from thread.model_commit import _require_pending_boundary

    _hold(blocked_input)
    _finish(blocked_input)
    stale = _request(blocked_input)
    attempts = []

    async def commit(request):
        attempts.append(request.operation_id)
        if len(attempts) > 1:
            return SimpleNamespace(status="committed")
        _request(blocked_input, "native/new-target", "replacement")
        _require_pending_boundary(
            _durable_thread_row(),
            SimpleNamespace(destination_model=stale.destination_model),
        )
        raise AssertionError("a moved boundary must not reach its commit")

    monkeypatch.setattr(model_pending, "_commit_fitting_switch", commit)
    monkeypatch.setattr(model_pending, "_drive_destination", AsyncMock())
    result = await model_pending.drive_pending_model_switch("held-thread")

    assert attempts == ["switch-held", "replacement"]
    assert result.status == "committed"
    assert blocked_input.execute(
        "SELECT switch_status,switch_error FROM threads WHERE id='held-thread'",
    ).fetchone()[0:2] == ("preparing", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_matches", [True, False])
async def test_only_obsolete_source_checkpoint_yields_its_slot(blocked_input, monkeypatch, source_matches):
    from thread import model_pending
    from infra import checkpoint_queries, checkpoints
    from core.checkpoint_types import CheckpointRequest
    from core import checkpoint_runtime

    _hold(blocked_input)
    _finish(blocked_input)
    request = _request(blocked_input)
    boundary = blocked_input.execute("SELECT id,seq FROM messages WHERE thread_id='held-thread' AND seq IS NOT NULL ORDER BY seq DESC LIMIT 1").fetchone()
    active = checkpoints.create_running(CheckpointRequest(
        id="old-checkpoint", thread_id="held-thread", history_epoch=0,
        previous_generation_id=None, through_seq=boundary["seq"], through_message_id=boundary["id"],
        trigger="pressure", mode="warm", source_harness="native",
        source_model="local/held" if source_matches else "unrelated/source",
        writer_harness="native", writer_model="local/held" if source_matches else "unrelated/source",
        originating_turn_id=None, first_included_seq=None, started_at="t1",
    ))
    monkeypatch.setattr(model_pending, "_checkpoint_state", lambda _request: (active, None))
    start = AsyncMock(return_value=(SimpleNamespace(status="running"), False))
    monkeypatch.setattr(model_pending, "_start_checkpoint", start)
    cancel = AsyncMock(wraps=checkpoint_runtime.cancel_checkpoint)
    monkeypatch.setattr(checkpoint_runtime, "cancel_checkpoint", cancel)
    assert await model_pending._resolve_required_checkpoint(request) == request
    if source_matches:
        cancel.assert_awaited_once_with("old-checkpoint")
        assert checkpoint_queries.read("old-checkpoint").status == "cancelled"
        start.assert_awaited_once_with(request, "cold")
    else:
        cancel.assert_not_awaited()
        start.assert_not_awaited()
        assert checkpoint_queries.read("old-checkpoint").status == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["fresh", "warm_failed", "obsolete_source"])
async def test_any_checkpoint_preparation_error_fails_the_switch_visibly(blocked_input, monkeypatch, state):
    """An unexpected preparation error ends the switch; it never escapes or stays preparing."""
    from thread import model_pending
    from thread.model_state import ModelSwitchCheckpointRequired
    from infra.model_switches import read_switch
    from core import checkpoint_runtime
    from thread import model_checkpoint

    _hold(blocked_input)
    _finish(blocked_input)
    _request(blocked_input)

    async def commit(_request):
        raise ModelSwitchCheckpointRequired(_request.source_model, _request.destination_model)

    active = SimpleNamespace(id="old-checkpoint") if state == "obsolete_source" else None
    attempt = SimpleNamespace(mode="warm", status="failed", error=None) if state == "warm_failed" else None
    monkeypatch.setattr(model_pending, "_commit_fitting_switch", commit)
    monkeypatch.setattr(model_pending, "_checkpoint_state", lambda _request: (active, attempt))
    monkeypatch.setattr(model_checkpoint, "obsolete_source_checkpoint", lambda _request, _active: True)
    monkeypatch.setattr(
        checkpoint_runtime, "cancel_checkpoint",
        AsyncMock(return_value=SimpleNamespace(status="cancelled")),
    )
    monkeypatch.setattr(
        model_pending, "_start_checkpoint",
        AsyncMock(side_effect=RuntimeError("checkpoint boundary is not a legal history boundary")),
    )
    monkeypatch.setattr(model_pending, "_drive_destination", AsyncMock())
    wake = AsyncMock()
    monkeypatch.setattr("infra.thread_execution.drive_thread", wake)

    from loguru import logger

    logged: list[str] = []
    sink = logger.add(lambda message: logged.append(str(message)), level="ERROR")
    try:
        result = await model_pending.drive_pending_model_switch("held-thread")
    finally:
        logger.remove(sink)

    assert result.status == "failed"
    assert any("Traceback" in line and "not a legal history boundary" in line for line in logged)
    wake.assert_awaited_once_with("held-thread")
    assert read_switch("held-thread") is None
    error = blocked_input.execute("SELECT switch_error FROM threads WHERE id='held-thread'").fetchone()[0]
    assert "not a legal history boundary" in error


@pytest.mark.asyncio
async def test_checkpoint_state_error_fails_the_switch_visibly(blocked_input, monkeypatch):
    from infra.model_switches import read_switch
    from loguru import logger
    from thread import model_pending
    from thread.model_state import ModelSwitchCheckpointRequired

    _hold(blocked_input)
    _finish(blocked_input)
    _request(blocked_input)

    async def checkpoint_required(request):
        raise ModelSwitchCheckpointRequired(request.source_model, request.destination_model)

    def checkpoint_state_error(_request):
        raise RuntimeError("checkpoint state is unavailable")

    monkeypatch.setattr(model_pending, "_commit_fitting_switch", checkpoint_required)
    monkeypatch.setattr(model_pending, "_checkpoint_state", checkpoint_state_error)
    wake = AsyncMock()
    monkeypatch.setattr("infra.thread_execution.drive_thread", wake)
    logged: list[str] = []
    sink = logger.add(lambda message: logged.append(str(message)), level="ERROR")
    try:
        result = await model_pending.drive_pending_model_switch("held-thread")
    finally:
        logger.remove(sink)

    assert result.status == "failed"
    assert read_switch("held-thread") is None
    assert "checkpoint state is unavailable" in blocked_input.execute(
        "SELECT switch_error FROM threads WHERE id='held-thread'",
    ).fetchone()[0]
    wake.assert_awaited_once_with("held-thread")
    assert any(
        "stage=required checkpoint resolution" in line
        and "Traceback" in line
        and "checkpoint state is unavailable" in line
        for line in logged
    )


@pytest.mark.asyncio
async def test_checkpoint_cancellation_error_fails_the_switch_visibly(blocked_input, monkeypatch):
    from core import checkpoint_runtime
    from infra.model_switches import read_switch
    from loguru import logger
    from thread import model_checkpoint, model_pending
    from thread.model_state import ModelSwitchCheckpointRequired

    _hold(blocked_input)
    _finish(blocked_input)
    _request(blocked_input)

    async def checkpoint_required(request):
        raise ModelSwitchCheckpointRequired(request.source_model, request.destination_model)

    async def cancellation_error(_checkpoint_id):
        raise RuntimeError("checkpoint cancellation failed")

    monkeypatch.setattr(model_pending, "_commit_fitting_switch", checkpoint_required)
    monkeypatch.setattr(
        model_pending, "_checkpoint_state", lambda _request: (SimpleNamespace(id="old-checkpoint"), None),
    )
    monkeypatch.setattr(model_checkpoint, "obsolete_source_checkpoint", lambda _request, _active: True)
    monkeypatch.setattr(checkpoint_runtime, "cancel_checkpoint", cancellation_error)
    wake = AsyncMock()
    monkeypatch.setattr("infra.thread_execution.drive_thread", wake)
    logged: list[str] = []
    sink = logger.add(lambda message: logged.append(str(message)), level="ERROR")
    try:
        result = await model_pending.drive_pending_model_switch("held-thread")
    finally:
        logger.remove(sink)

    assert result.status == "failed"
    assert read_switch("held-thread") is None
    assert "checkpoint cancellation failed" in blocked_input.execute(
        "SELECT switch_error FROM threads WHERE id='held-thread'",
    ).fetchone()[0]
    wake.assert_awaited_once_with("held-thread")
    assert any("Traceback" in line and "checkpoint cancellation failed" in line for line in logged)


@pytest.mark.asyncio
async def test_untyped_commit_error_fails_the_switch_visibly(blocked_input, monkeypatch):
    from infra.model_switches import read_switch
    from loguru import logger
    from thread import model_pending

    _hold(blocked_input)
    _finish(blocked_input)
    _request(blocked_input)

    async def commit_error(_request):
        raise RuntimeError("commit preparation failed")

    monkeypatch.setattr(model_pending, "_commit_fitting_switch", commit_error)
    wake = AsyncMock()
    monkeypatch.setattr("infra.thread_execution.drive_thread", wake)
    logged: list[str] = []
    sink = logger.add(lambda message: logged.append(str(message)), level="ERROR")
    try:
        result = await model_pending.drive_pending_model_switch("held-thread")
    finally:
        logger.remove(sink)

    assert result.status == "failed"
    assert read_switch("held-thread") is None
    assert "commit preparation failed" in blocked_input.execute(
        "SELECT switch_error FROM threads WHERE id='held-thread'",
    ).fetchone()[0]
    wake.assert_awaited_once_with("held-thread")
    assert any(
        "stage=commit attempt" in line
        and "Traceback" in line
        and "commit preparation failed" in line
        for line in logged
    )


@pytest.mark.asyncio
async def test_destination_error_after_commit_does_not_fail_the_switch(blocked_input, monkeypatch):
    from infra.model_switches import read_switch
    from thread import model_pending

    _hold(blocked_input)
    _finish(blocked_input)
    _request(blocked_input)

    async def commit(request):
        blocked_input.execute(
            "UPDATE threads SET model=pending_model,pending_model=NULL,"
            "pending_execution_epoch=NULL,switch_after_input_order=NULL,"
            "switch_operation_id=NULL,switch_status=NULL,switch_error=NULL "
            "WHERE id=?",
            (request.thread_id,),
        )
        blocked_input.commit()
        return SimpleNamespace(status="committed")

    async def destination_error(_thread_id):
        raise RuntimeError("destination wake failed")

    monkeypatch.setattr(model_pending, "_commit_fitting_switch", commit)
    monkeypatch.setattr(model_pending, "_drive_destination", destination_error)

    with pytest.raises(RuntimeError, match="destination wake failed"):
        await model_pending.drive_pending_model_switch("held-thread")

    assert read_switch("held-thread") is None
    assert blocked_input.execute(
        "SELECT model,switch_status,switch_error FROM threads WHERE id='held-thread'",
    ).fetchone()[0:3] == ("native/destination", None, None)


@pytest.mark.asyncio
async def test_source_wake_error_after_preparation_failure_propagates_once(blocked_input, monkeypatch):
    """A failed switch's source wake is not another preparation failure."""
    from infra.model_switches import read_switch
    from thread import model_pending
    from thread.model_state import ModelSwitchConflict

    _hold(blocked_input)
    _finish(blocked_input)
    _request(blocked_input)

    async def commit_error(_request):
        raise ModelSwitchConflict("commit proof disappeared")

    async def source_wake_error(_thread_id):
        raise RuntimeError("source wake failed")

    monkeypatch.setattr(model_pending, "_commit_fitting_switch", commit_error)
    wake = AsyncMock(side_effect=source_wake_error)
    monkeypatch.setattr("infra.thread_execution.drive_thread", wake)

    with pytest.raises(RuntimeError, match="source wake failed"):
        await model_pending.drive_pending_model_switch("held-thread")

    wake.assert_awaited_once_with("held-thread")
    assert read_switch("held-thread") is None
    assert "commit proof disappeared" in blocked_input.execute(
        "SELECT switch_error FROM threads WHERE id='held-thread'",
    ).fetchone()[0]


@pytest.mark.asyncio
async def test_destination_wake_after_sequencing_reread_propagates_once(blocked_input, monkeypatch):
    """The healthy FIFO head owns its wake even when its reread follows a race."""
    from dataclasses import replace

    from infra import model_switches
    from infra.model_switches import read_switch
    from thread import model_pending, model_runtime
    from thread.model_state import ModelSwitchSequencingRequired

    _hold(blocked_input)
    _finish(blocked_input)
    request = _request(blocked_input)
    rereads = 0

    def advance_pending_switch(_thread_id):
        nonlocal rereads
        rereads += 1
        return request if rereads == 1 else replace(request, waiting_for_source=True)

    async def sequencing_required(_request):
        raise ModelSwitchSequencingRequired("FIFO head changed")

    async def destination_wake_error(_thread_id):
        raise RuntimeError("destination wake failed after reread")

    monkeypatch.setattr(model_switches, "advance_pending_switch", advance_pending_switch)
    monkeypatch.setattr(model_pending, "_commit_fitting_switch", sequencing_required)
    monkeypatch.setattr(model_runtime, "broadcast_switch_inputs", AsyncMock())
    destination = AsyncMock(side_effect=destination_wake_error)
    monkeypatch.setattr(model_pending, "_drive_destination", destination)
    source_wake = AsyncMock()
    monkeypatch.setattr("infra.thread_execution.drive_thread", source_wake)

    with pytest.raises(RuntimeError, match="destination wake failed after reread"):
        await model_pending.drive_pending_model_switch("held-thread")

    destination.assert_awaited_once_with("held-thread")
    source_wake.assert_not_awaited()
    assert read_switch("held-thread").operation_id == request.operation_id
