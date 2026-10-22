"""An accepted send always returns its receipt; later failures stay on the input."""

from __future__ import annotations

import logging
import sqlite3
from unittest.mock import AsyncMock

import pytest

_MODEL = "anthropic/claude-3-5-sonnet"


def _locked(*_args, **_kwargs):
    raise sqlite3.OperationalError("database is locked")


def _unavailable(*_args, **_kwargs):
    raise RuntimeError("model_unavailable")


def _error_of(failure):
    try:
        failure()
    except Exception as error:
        return error


def _thread(thread_id: str = "thread-1") -> dict:
    from infra.database import get_db

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        f"VALUES ('{thread_id}','user-1','{_MODEL}','t0','t0')"
    )
    db.commit()
    row = dict(db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone())
    db.close()
    return row


def _state(message_id: str) -> tuple[str, str | None]:
    from infra.input_admission import input_delivery

    return input_delivery(message_id)


def _rows(message_id: str) -> int:
    from infra.database import get_db

    db = get_db()
    try:
        return db.execute(
            "SELECT COUNT(*) FROM messages WHERE id=?", (message_id,),
        ).fetchone()[0]
    finally:
        db.close()


def _turn_status(turn_id: str) -> str:
    from infra.database import get_db

    db = get_db()
    try:
        return db.execute(
            "SELECT status FROM active_turns WHERE id=?", (turn_id,),
        ).fetchone()[0]
    finally:
        db.close()


def _stub_delivery(monkeypatch):
    import infra.send_service as service

    executed = []

    async def _execute(batch, **_kwargs):
        executed.append(batch)
        return service.SendResult(
            driver="fake", message_id=batch.items[0].message_id,
            turn_id=batch.turn_id, turn_order=batch.turn_order,
        )

    monkeypatch.setattr(service, "execute_claimed_batch", _execute)
    monkeypatch.setattr(service, "request_safe_boundary", AsyncMock())
    return service, executed


async def _send(service, row, input_id, **kwargs):
    return await service.send_thread_input(
        "thread-1", row, input_id, user={"id": "user-1"}, input_id=input_id,
        native_mode="ws", ws=object(), **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["broadcast_accepted_input", "broadcast_newest_error", "on_admitted"])
@pytest.mark.parametrize("failure", [_locked, _unavailable])
async def test_idle_send_still_dispatches_when_projection_step_fails(
    isolated_db, monkeypatch, caplog, step, failure,
) -> None:
    service, executed = _stub_delivery(monkeypatch)
    row = _thread()
    kwargs = {}
    if step == "on_admitted":
        kwargs["on_admitted"] = AsyncMock(side_effect=failure)
    else:
        monkeypatch.setattr(service, step, failure)

    with caplog.at_level(logging.WARNING, logger="infra.send_service"):
        result = await _send(service, row, "input-1", **kwargs)

    assert isinstance(result, service.SendResult)
    assert len(executed) == 1
    assert "input-1" in [item.message_id for item in executed[0].items]
    logged = [r for r in caplog.records if "post-admission" in r.getMessage()]
    assert len(logged) == 1
    assert "input input-1" in logged[0].getMessage()
    assert logged[0].exc_info is not None  # a traceback, not a class name
    assert type(_error_of(failure)) is logged[0].exc_info[0]
    assert _rows("input-1") == 1


@pytest.mark.asyncio
async def test_claimed_target_failure_fails_input_and_answers_like_a_retry(
    isolated_db, monkeypatch,
) -> None:
    """The claimed Turn must not strand, and the sender is not told 'queued'."""
    service, executed = _stub_delivery(monkeypatch)
    row = _thread()
    monkeypatch.setattr(service, "_target_for", AsyncMock(side_effect=_unavailable))

    with pytest.raises(service.SendRejected) as first:
        await _send(service, row, "input-1")
    with pytest.raises(service.SendRejected) as retry:
        await _send(service, row, "input-1")

    assert first.value.input_undeliverable is True
    assert "model_unavailable" in first.value.detail
    assert (first.value.detail, first.value.metadata) == (
        retry.value.detail, retry.value.metadata,
    )
    assert executed == []
    state, error = _state("input-1")
    assert state == "failed" and "model_unavailable" in (error or "")
    assert _rows("input-1") == 1


@pytest.mark.asyncio
async def test_settlement_failure_after_target_failure_propagates_chained(
    isolated_db, monkeypatch,
) -> None:
    import core.turn_core as turn_core

    service, _executed = _stub_delivery(monkeypatch)
    row = _thread()
    monkeypatch.setattr(service, "_target_for", AsyncMock(side_effect=_unavailable))
    monkeypatch.setattr(turn_core, "settle_turn_terminal", AsyncMock(side_effect=_locked))

    with pytest.raises(sqlite3.OperationalError) as raised:
        await _send(service, row, "input-1")

    assert isinstance(raised.value.__cause__, RuntimeError)


@pytest.mark.asyncio
async def test_idle_pending_switch_wake_failure_is_not_a_queued_receipt(
    isolated_db, monkeypatch,
) -> None:
    """Nothing else wakes an idle thread's held input, so the error propagates."""
    import infra.thread_driver as thread_driver
    import thread.model_pending as pending

    service, _executed = _stub_delivery(monkeypatch)
    row = _thread()
    monkeypatch.setattr(thread_driver, "start_idle_turn", lambda *a, **k: None)
    monkeypatch.setattr(
        pending, "drive_pending_model_switch", AsyncMock(side_effect=_locked),
    )

    with pytest.raises(sqlite3.OperationalError):
        await _send(service, row, "input-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [_locked, _unavailable])
async def test_safe_boundary_wake_failure_keeps_receipt_and_queue(
    isolated_db, monkeypatch, caplog, failure,
) -> None:
    service, executed = _stub_delivery(monkeypatch)
    row = _thread()
    first = await _send(service, row, "input-1")
    monkeypatch.setattr(
        service, "request_safe_boundary", AsyncMock(side_effect=failure),
    )

    with caplog.at_level(logging.WARNING, logger="infra.send_service"):
        queued = await _send(service, row, "input-2")

    assert queued.receipt.message_id == "input-2"
    assert any(
        "input input-2" in r.getMessage() and "post-admission" in r.getMessage()
        for r in caplog.records
    )
    assert _state("input-2")[0] == "queued"
    retry = await _send(service, row, "input-2")
    assert retry.duplicate is True
    assert _rows("input-2") == 1
    assert _turn_status(first.turn_id) == "running"
