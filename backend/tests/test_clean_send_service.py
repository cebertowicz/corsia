"""Shared sender integration contracts over the clean thread driver."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_missing_thread_is_a_public_not_found(isolated_db) -> None:
    from infra.send_service import SendRejected, send_thread_input

    with pytest.raises(SendRejected) as rejected:
        await send_thread_input(
            "missing", {}, "hello", native_mode="ws",
        )

    assert rejected.value.status_code == 404
    assert rejected.value.detail == "thread does not exist"


@pytest.mark.asyncio
async def test_idle_send_dispatches_and_busy_send_requests_one_safe_boundary(
    isolated_db, monkeypatch,
) -> None:
    """Send never rejects a busy thread; it persists first and requests steer."""
    from infra.database import get_db
    import infra.send_service as service
    from infra.send_types import InboxQueuedInput, SendResult
    from llm.resolution import resolve_execution_target

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','anthropic/claude-3-5-sonnet','t0','t0')"
    )
    db.commit()
    thread_row = dict(db.execute(
        "SELECT * FROM threads WHERE id='thread-1'"
    ).fetchone())
    db.close()
    target = resolve_execution_target("anthropic/claude-3-5-sonnet")

    dispatched = []
    boundaries = []

    async def _execute(batch, **_kwargs):
        dispatched.append(batch)
        return service.SendResult(
            driver="fake", message_id=batch.items[0].message_id,
            turn_id=batch.turn_id, turn_order=batch.turn_order,
        )

    async def _boundary(thread_id: str, target_harness: str) -> None:
        boundaries.append((thread_id, target_harness))

    monkeypatch.setattr(service, "execute_claimed_batch", _execute, raising=False)
    monkeypatch.setattr(service, "request_safe_boundary", _boundary, raising=False)
    monkeypatch.setattr(service, "broadcast_accepted_input", lambda *_a, **_k: None, raising=False)

    first = await service.send_thread_input(
        "thread-1", thread_row, "first", user={"id": "user-1"},
        input_id="input-1", native_mode="ws", target=target, ws=object(),
    )
    second = await service.send_thread_input(
        "thread-1", thread_row, "second", user={"id": "user-1"},
        input_id="input-2", native_mode="ws", target=target, ws=object(),
    )

    assert isinstance(first, SendResult)
    assert [batch.turn_id for batch in dispatched] == [first.turn_id]
    assert isinstance(second, InboxQueuedInput)
    assert second.receipt.message_id == "input-2"
    assert boundaries == [("thread-1", "native")]


@pytest.mark.asyncio
async def test_cancelled_queued_input_retry_reports_undeliverable(
    isolated_db, monkeypatch,
) -> None:
    """A same-key retry reports the cancelled row's durable delivery outcome."""
    from infra.database import get_db
    from infra.send_service import SendRejected, send_thread_input
    from infra.queued_input_settlement import cancel_queued_input
    import infra.send_service as service

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','anthropic/claude-3-5-sonnet','t0','t0')"
    )
    db.commit()
    thread_row = dict(db.execute(
        "SELECT * FROM threads WHERE id='thread-1'"
    ).fetchone())
    db.close()

    async def _execute(batch, **_kwargs):
        return service.SendResult(
            driver="fake", message_id=batch.items[0].message_id,
            turn_id=batch.turn_id, turn_order=batch.turn_order,
        )

    monkeypatch.setattr(service, "execute_claimed_batch", _execute)
    monkeypatch.setattr(service, "request_safe_boundary", AsyncMock())
    monkeypatch.setattr(service, "broadcast_accepted_input", lambda *_a, **_k: None)

    await send_thread_input(
        "thread-1", thread_row, "first", user={"id": "user-1"},
        input_id="input-1", native_mode="ws", ws=object(),
    )
    queued = await send_thread_input(
        "thread-1", thread_row, "second", user={"id": "user-1"},
        input_id="input-2", native_mode="ws", ws=object(),
    )
    assert queued.receipt.message_id == "input-2"
    db = get_db()
    try:
        row = db.execute(
            "SELECT delivery_revision FROM messages WHERE id='input-2'"
        ).fetchone()
        cancelled = cancel_queued_input(
            "thread-1", "input-2", revision=int(row["delivery_revision"]), db=db,
        )
        assert cancelled["delivery_state"] == "cancelled"
    finally:
        db.close()

    with pytest.raises(SendRejected) as rejected:
        await send_thread_input(
            "thread-1", thread_row, "second", user={"id": "user-1"},
            input_id="input-2", native_mode="ws", ws=object(),
        )
    assert rejected.value.status_code == 409
    assert rejected.value.input_undeliverable
    assert rejected.value.metadata["cause"] == "input_delivery_failed"


@pytest.mark.asyncio
async def test_next_send_drives_pending_switch_once_and_keeps_one_input(
    isolated_db,
    monkeypatch,
) -> None:
    """Activity, not startup, resumes one pending switch over one FIFO row."""
    from infra.database import get_db
    from infra.model_switches import request_switch
    import infra.send_service as service
    from infra.send_types import InboxQueuedInput
    from thread import model_pending
    from thread.model_state import ModelSwitchCheckpointRequired

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
            "VALUES ('thread-1','user-1','native/source','t0','t0')"
        )
        request = request_switch(
            "thread-1",
            "native/target",
            operation_id="switch-1",
            requested_at="t1",
            db=db,
        )
        db.commit()
        thread_row = dict(
            db.execute("SELECT * FROM threads WHERE id='thread-1'").fetchone()
        )
    finally:
        db.close()

    admissions: list[str] = []

    async def require_checkpoint(_request):
        raise ModelSwitchCheckpointRequired(
            _request.source_model, _request.destination_model,
        )

    async def start_checkpoint(_request, mode: str):
        admissions.append(mode)
        return (None, False) if mode == "warm" else (object(), False)

    monkeypatch.setattr(model_pending, "_commit_fitting_switch", require_checkpoint)
    monkeypatch.setattr(model_pending, "_checkpoint_state", lambda _request: (None, None))
    monkeypatch.setattr(model_pending, "_start_checkpoint", start_checkpoint)
    monkeypatch.setattr(
        service, "broadcast_accepted_input", lambda *_args, **_kwargs: None
    )

    result = await service.send_thread_input(
        "thread-1",
        thread_row,
        "continue after restart",
        user={"id": "user-1"},
        input_id="input-1",
        native_mode="ws",
        ws=object(),
    )

    assert isinstance(result, InboxQueuedInput)
    assert admissions == ["warm", "cold"]
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id,content,metadata,delivery_state,active_turn_id,target_model "
            "FROM messages "
            "WHERE thread_id='thread-1' ORDER BY input_order"
        ).fetchall()
        assert len(rows) == 2
        assert "[PASSIVE CONTEXT DIGEST]" in rows[0]["content"]
        assert json.loads(rows[0]["metadata"])["event_type"] == "context.digest"
        assert (
            rows[1]["id"], rows[1]["delivery_state"],
            rows[1]["active_turn_id"], rows[1]["target_model"],
        ) == ("input-1", "queued", None, "native/target")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unavailable_model_holds_fifo_head_through_real_idle_send(
    isolated_db, monkeypatch,
) -> None:
    """An unavailable model's real idle send holds the FIFO head with the typed
    unavailable cause, never the checkpoint-boundary error a raw exception
    used to surface at admission."""
    from infra.database import get_db
    import infra.send_service as service
    from infra.send_types import InboxQueuedInput
    from tests.unavailable_model_support import seed_model

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','removed/model','t0','t0')"
    )
    db.commit()
    seed_model("removed/model", available=0)
    thread_row = dict(db.execute(
        "SELECT * FROM threads WHERE id='thread-1'"
    ).fetchone())
    db.close()

    monkeypatch.setattr(service, "broadcast_accepted_input", lambda *_a, **_k: None)

    result = await service.send_thread_input(
        "thread-1", thread_row, "hello", user={"id": "user-1"},
        input_id="input-1", native_mode="ws", ws=object(),
    )

    assert isinstance(result, InboxQueuedInput)
    assert result.receipt.message_id == "input-1"
    db = get_db()
    try:
        assert tuple(db.execute(
            "SELECT delivery_state,write_outcome,delivery_error,active_turn_id "
            "FROM messages WHERE id='input-1'"
        ).fetchone()) == (
            "queued", "proved_no_write", "selected_model_unavailable", None,
        )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_target_invalid_is_not_held_and_fails_visibly(
    isolated_db, monkeypatch,
) -> None:
    """A malformed target configuration still fails visibly; it is never
    mistaken for a recoverable unavailable-model hold."""
    from infra.database import get_db
    from infra.send_target import SELECTED_MODEL_UNAVAILABLE, TargetInvalid
    import infra.send_service as service

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','broken/model','t0','t0')"
    )
    db.commit()
    thread_row = dict(db.execute(
        "SELECT * FROM threads WHERE id='thread-1'"
    ).fetchone())
    db.close()

    async def invalid(_model):
        raise TargetInvalid("ambiguous model transport identity: broken/model")

    monkeypatch.setattr("infra.send_target.resolve_target", invalid)
    monkeypatch.setattr(service, "broadcast_accepted_input", lambda *_a, **_k: None)

    with pytest.raises(service.SendRejected) as rejected:
        await service.send_thread_input(
            "thread-1", thread_row, "hello", user={"id": "user-1"},
            input_id="input-1", native_mode="ws", ws=object(),
        )

    db = get_db()
    try:
        row = db.execute(
            "SELECT delivery_state,write_outcome,delivery_error,active_turn_id "
            "FROM messages WHERE id='input-1'"
        ).fetchone()
    finally:
        db.close()
    assert rejected.value.input_undeliverable is True
    assert "ambiguous model transport identity" in rejected.value.detail
    assert row["delivery_state"] == "failed"
    assert row["delivery_error"] != SELECTED_MODEL_UNAVAILABLE
