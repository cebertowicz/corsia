"""Routed-event lifecycle coverage for the canonical native turn engine."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock

import pytest

from shared_send_fixtures import _install_fake_native_context_agent


def _native_target():
    from llm.resolution import ExecutionTarget

    return ExecutionTarget(
        catalog_id="opus",
        harness="native",
        provider="anthropic",
        backend="api",
        transport_model="anthropic/test",
        expected_reported_model=None,
        context_window=200_000,
        available=True,
        availability_reason=None,
        display_name="Test Model",
        supports_reasoning=False,
    )


def _seed_event_thread_row(thread_id: str, *, user_id: str = "owner-1") -> dict:
    from infra.database import get_db

    with closing(get_db()) as db:
        db.execute(
            "INSERT INTO threads "
            "(id, user_id, title, model, project_id, created_at, updated_at) "
            "VALUES (?, ?, 'T', ?, ?, 't', 't')",
            (thread_id, user_id, "opus", None),
        )
        db.commit()
        return dict(
            db.execute(
                "SELECT * FROM threads WHERE id=?",
                (thread_id,),
            ).fetchone()
        )


def _turn_status(thread_id: str) -> str | None:
    from infra.database import get_db

    with closing(get_db()) as db:
        row = db.execute(
            "SELECT status FROM active_turns WHERE thread_id = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
    return row["status"] if row else None


def _message_counts(thread_id: str) -> tuple[int, int, int]:
    from infra.database import get_db

    with closing(get_db()) as db:
        input_count = db.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id = ? AND role = 'user'",
            (thread_id,),
        ).fetchone()[0]
        assistant_count = db.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id = ? AND role = 'assistant'",
            (thread_id,),
        ).fetchone()[0]
        chip_count = db.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id = ? "
            "AND role = 'system' AND metadata LIKE '%\"context_event\"%' "
            "AND COALESCE(json_extract(metadata,'$.event_type'),'') "
            "!= 'context.digest'",
            (thread_id,),
        ).fetchone()[0]
    return input_count, assistant_count, chip_count


def _install_event_engine(
    monkeypatch,
    tmp_path,
    *,
    run_turn,
    product_events: list,
    post_turn: AsyncMock,
):
    from core import native_turn_runtime
    from ws import turn as ws_turn

    _install_fake_native_context_agent(monkeypatch)
    monkeypatch.setattr(ws_turn, "make_provider_params", lambda _model: {})
    monkeypatch.setattr(native_turn_runtime, "run_turn_with_steps", run_turn)
    monkeypatch.setattr(ws_turn, "run_post_turn_tasks", post_turn)
    monkeypatch.setattr(
        ws_turn,
        "emit_product_event",
        lambda category, action, **kwargs: product_events.append(
            (category, action, kwargs, _turn_status(kwargs["thread_id"]))
        ),
    )
    return ws_turn


@pytest.mark.asyncio
async def test_automatic_input_delivers_without_obsolete_write_receipt(
    isolated_db, monkeypatch, tmp_path,
):
    from infra import state
    from infra.send_service import send_thread_input

    thread_id = "automatic-input-without-summary-writer"
    thread_row = _seed_event_thread_row(thread_id)
    provider_inputs = []

    async def provider(*, user_content, on_provider_response, on_step, **_kwargs):
        provider_inputs.append(user_content)
        await on_provider_response()
        await on_step("text", {"content": "Automatic work completed"})
        await on_step("turn_complete", {"context_usage": None})

    _install_event_engine(monkeypatch, tmp_path, run_turn=provider,
                          product_events=[], post_turn=AsyncMock())
    with closing(sqlite3.connect(str(isolated_db))) as db:

        db.execute("CREATE TRIGGER reject_unneeded_failure_receipt BEFORE UPDATE OF write_outcome "
                   "ON messages WHEN NEW.write_outcome='proved_no_write' "
                   "BEGIN SELECT RAISE(FAIL, 'unneeded failure receipt'); END")
        db.commit()

    result = await send_thread_input(
        thread_id, thread_row, "Do the unattended work", role="system",
        metadata={"type": "context_event"}, native_mode="event", target=_native_target(),
    )
    assert result.turn_task is not None
    assert await result.turn_task is False
    assert len(provider_inputs) == 1
    assert "Do the unattended work" in provider_inputs[0]
    with closing(sqlite3.connect(str(isolated_db))) as db:
        assert db.execute(
            "SELECT delivery_state,write_outcome FROM messages "
            "WHERE thread_id=? AND content='Do the unattended work'", (thread_id,),
        ).fetchall() == [("delivered", "accepted")]

    assert _turn_status(thread_id) == "completed"
    assert thread_id not in state._active_turns
    assert thread_id not in state._active_turn_tasks


def test_record_browser_turn_activity_uses_thread_state():
    from infra import state
    from ws.turn_helpers import record_browser_turn_activity

    thread_id = "browser-activity-thread"
    activity_key = f"web:owner-1:{thread_id}"
    record_browser_turn_activity(thread_id, "owner-1", "omlx@srv/model")

    try:
        model_state = state._active_thread_models[thread_id]
        assert model_state == {
            "model_id": "omlx@srv/model",
            "last_active": state._browser_thread_last_activity[activity_key],
            "ws_connected": True,
        }
    finally:
        state._active_thread_models.pop(thread_id, None)
        state._browser_thread_last_activity.pop(activity_key, None)



@pytest.mark.asyncio
async def test_event_success_uses_d3_lifecycle_once(
    isolated_db,
    monkeypatch,
    tmp_path,
):
    from core import step_handler
    from infra import state
    from infra.database import get_db
    from infra.send_service import send_thread_input
    from infra.state import turn_buffers

    thread_id = "event-success"
    thread_row = _seed_event_thread_row(thread_id)
    product_events: list[tuple] = []
    provider_call: dict = {}
    broadcasts: list[dict] = []
    handler_kwargs: dict = {}

    async def _run_turn(*, on_step, on_provider_response, **kwargs):
        provider_call.update(kwargs)
        await on_provider_response()
        await on_step("text", {"content": "event response"})
        await on_step("turn_complete", {"context_usage": None})

    post_turn = AsyncMock()
    ws_turn = _install_event_engine(
        monkeypatch,
        tmp_path,
        run_turn=_run_turn,
        product_events=product_events,
        post_turn=post_turn,
    )
    real_handler = step_handler.StepHandler

    def _handler(**kwargs):
        handler_kwargs.update(kwargs)
        return real_handler(**kwargs)

    monkeypatch.setattr(ws_turn, "StepHandler", _handler)
    seen_identity = []

    def _owner_broadcast(_thread_id, _owner_id, message: dict) -> None:
        broadcasts.append(message)
        if "turn_id" in message:
            from core.turn_core import TurnIdentity

            seen_identity.append(
                TurnIdentity(
                    message["turn_id"],
                    message["turn_order"],
                )
            )

    monkeypatch.setattr(state, "broadcast_event", _owner_broadcast)

    try:
        result = await send_thread_input(
            thread_id,
            thread_row,
            "event content",
            role="system",
            metadata={
                "type": "context_event",
                "routed_event": "webhook.github.pr_opened",
            },
            native_mode="event",
            target=_native_target(),
        )
        assert result.turn_task is not None
        cancelled = await result.turn_task
    finally:
        state._active_thread_models.pop(thread_id, None)
        state._browser_thread_last_activity.pop(f"web:owner-1:{thread_id}", None)
        if seen_identity:
            turn_buffers.clear(thread_id, seen_identity[0])

    assert cancelled is False
    assert [
        (category, action, kwargs["thread_id"], kwargs["user_id"], status)
        for category, action, kwargs, status in product_events
    ] == [
        ("turn", "started", thread_id, "owner-1", "running"),
        ("turn", "completed", thread_id, "owner-1", "completed"),
    ]
    post_turn.assert_awaited_once()
    post_kwargs = post_turn.await_args.kwargs
    assert post_kwargs["thread_id"] == thread_id
    provider_input = post_kwargs["content"]
    assert provider_input.startswith(
        "CORSIA is delivering 2 queued inbound inputs together."
    )
    assert "[1/2]\n[PASSIVE CONTEXT DIGEST]" in provider_input
    assert provider_input.endswith("[2/2]\nevent content")
    assert callable(post_kwargs["ws_send"])
    assert handler_kwargs["broadcast"] is post_kwargs["ws_send"]
    assert handler_kwargs["extra_meta"]["routed_event"] == ("webhook.github.pr_opened")
    assert provider_call["user_content"] == provider_input
    assert provider_call["media"] is None
    assert provider_call["reasoning_effort"] is None
    assert thread_id not in state._active_thread_models
    assert f"web:owner-1:{thread_id}" not in state._browser_thread_last_activity
    assert seen_identity
    assert all(identity == seen_identity[0] for identity in seen_identity)
    assert turn_buffers.snapshot(thread_id, seen_identity[0]) is None
    assert [message["type"] for message in broadcasts] == [
        "turn_activity",
        "step_text",
        "turn_complete",
        "session_updated",
    ]
    # The native all-ingress admission frame names the running Turn's visible
    # root (the thread itself for a chat row): the same lineage contract the
    # subagent runtimes attach, so a client can attribute any running Turn.
    assert broadcasts[0]["visible_root_thread_id"] == thread_id
    assert broadcasts[0]["started_at"] is not None
    with closing(get_db()) as db:
        row = db.execute(
            "SELECT metadata FROM messages WHERE thread_id = ? AND role = 'assistant'",
            (thread_id,),
        ).fetchone()
    assert json.loads(row["metadata"])["routed_event"] == ("webhook.github.pr_opened")


@pytest.mark.asyncio
async def test_event_soft_error_terminalizes_once_and_preserves_accepted_input(
    isolated_db,
    monkeypatch,
    tmp_path,
):
    from core.turn_iteration import TurnProviderError
    from infra import state
    from infra.send_service import send_thread_input

    thread_id = "event-soft-error"
    thread_row = _seed_event_thread_row(thread_id)
    product_events: list[tuple] = []
    broadcasts: list[dict] = []

    async def _run_turn(*, on_step, on_provider_response, **_kwargs):
        await on_provider_response()
        await on_step("error", {"message": "provider failed"})
        raise TurnProviderError("provider failed")

    post_turn = AsyncMock()
    _install_event_engine(
        monkeypatch,
        tmp_path,
        run_turn=_run_turn,
        product_events=product_events,
        post_turn=post_turn,
    )
    monkeypatch.setattr(
        state,
        "broadcast_event",
        lambda _sid, _owner, message: broadcasts.append(message),
    )

    result = await send_thread_input(
        thread_id,
        thread_row,
        "event content",
        role="system",
        metadata={"type": "context_event"},
        native_mode="event",
        target=_native_target(),
    )
    assert result.turn_task is not None
    with pytest.raises(TurnProviderError, match="provider failed"):
        await result.turn_task

    assert _turn_status(thread_id) == "error"
    assert [event[1] for event in product_events] == ["started", "errored"]
    assert [
        message["message"] for message in broadcasts if message.get("type") == "error"
    ] == ["provider failed"]
    post_turn.assert_not_awaited()
    assert _message_counts(thread_id) == (0, 0, 1)


@pytest.mark.asyncio
async def test_event_generic_error_preserves_accepted_input_without_assistant(
    isolated_db,
    monkeypatch,
    tmp_path,
):
    from infra import state
    from infra.send_service import send_thread_input

    thread_id = "event-generic-error"
    thread_row = _seed_event_thread_row(thread_id)
    product_events: list[tuple] = []
    broadcasts: list[dict] = []

    async def _run_turn(*, on_provider_response, **_kwargs):
        await on_provider_response()
        raise RuntimeError("turn engine exploded")

    post_turn = AsyncMock()
    _install_event_engine(
        monkeypatch,
        tmp_path,
        run_turn=_run_turn,
        product_events=product_events,
        post_turn=post_turn,
    )
    monkeypatch.setattr(
        state,
        "broadcast_event",
        lambda _sid, _owner, message: broadcasts.append(message),
    )

    result = await send_thread_input(
        thread_id,
        thread_row,
        "event content",
        role="system",
        metadata={"type": "context_event"},
        native_mode="event",
        target=_native_target(),
    )
    assert result.turn_task is not None
    with pytest.raises(RuntimeError, match="turn engine exploded"):
        await result.turn_task

    assert _turn_status(thread_id) == "error"
    assert [event[1] for event in product_events] == ["started", "errored"]
    assert [m for m in broadcasts if m.get("type") == "error"] == []
    post_turn.assert_not_awaited()
    assert _message_counts(thread_id) == (0, 0, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_site", "message"),
    [
        ("model", "not available"),
        ("agent", "agent unavailable"),
        ("broadcast", "owner broadcast"),
        ("identity", "does not match"),
        ("bind", "bind failed"),
    ],
)
async def test_preadmitted_setup_failure_terminalizes_and_admits_next_input(
    isolated_db,
    monkeypatch,
    tmp_path,
    failure_site,
    message,
):
    from core.turn_core import TurnIdentity, TurnNotPreadmittedError
    from infra.send_service import send_thread_input
    from llm.resolution import ModelUnavailableError

    thread_id = f"event-setup-failure-{failure_site}"
    thread_row = _seed_event_thread_row(thread_id)
    post_turn = AsyncMock()

    async def run_turn(*, on_provider_response, on_step, **_kwargs):
        await on_provider_response()
        await on_step("text", {"content": "next input completed"})
        await on_step("turn_complete", {"context_usage": None})

    ws_turn = _install_event_engine(
        monkeypatch,
        tmp_path,
        run_turn=run_turn,
        product_events=[],
        post_turn=post_turn,
    )
    restore = None
    if failure_site == "model":
        original = ws_turn.validate_native_target

        async def fail_target(*_args, **_kwargs):
            raise ModelUnavailableError("anthropic/test", "not available")

        monkeypatch.setattr(ws_turn, "validate_native_target", fail_target)
        restore = lambda: monkeypatch.setattr(ws_turn, "validate_native_target", original)
        expected_type = ModelUnavailableError
    elif failure_site == "agent":
        original = ws_turn._setup_turn

        async def fail_get_agent(*_args, **_kwargs):
            raise RuntimeError("agent unavailable")

        monkeypatch.setattr(ws_turn, "_setup_turn", fail_get_agent)
        restore = lambda: monkeypatch.setattr(ws_turn, "_setup_turn", original)
        expected_type = RuntimeError
    elif failure_site == "broadcast":
        original = ws_turn._setup_turn

        async def missing_broadcast(*args, **kwargs):
            kwargs["broadcast"] = None
            return await original(*args, **kwargs)

        monkeypatch.setattr(ws_turn, "_setup_turn", missing_broadcast)
        restore = lambda: monkeypatch.setattr(ws_turn, "_setup_turn", original)
        expected_type = ValueError
    elif failure_site == "identity":
        original = ws_turn._setup_turn

        async def mismatched_identity(*args, **kwargs):
            kwargs["preadmitted_turn"] = TurnIdentity("wrong-turn", 1)
            return await original(*args, **kwargs)

        monkeypatch.setattr(ws_turn, "_setup_turn", mismatched_identity)
        restore = lambda: monkeypatch.setattr(ws_turn, "_setup_turn", original)
        expected_type = ValueError
    else:
        original = ws_turn.bind_preadmitted_turn

        async def fail_bind(*_args, **_kwargs):
            raise TurnNotPreadmittedError("bind failed")

        monkeypatch.setattr(ws_turn, "bind_preadmitted_turn", fail_bind)
        restore = lambda: monkeypatch.setattr(ws_turn, "bind_preadmitted_turn", original)
        expected_type = TurnNotPreadmittedError

    with pytest.raises(expected_type, match=message):
        await send_thread_input(
            thread_id,
            thread_row,
            "setup failure input",
            role="system",
            metadata={"type": "context_event"},
            native_mode="event",
            target=_native_target(),
        )

    assert _turn_status(thread_id) == "error"
    restore()
    result = await send_thread_input(
        thread_id,
        thread_row,
        "next queued input",
        role="system",
        metadata={"type": "context_event"},
        native_mode="event",
        target=_native_target(),
    )

    assert result.turn_task is not None
    assert await result.turn_task is False
    assert _turn_status(thread_id) == "completed"
    with closing(sqlite3.connect(str(isolated_db))) as db:
        assert db.execute(
            "SELECT content, delivery_state FROM messages "
            "WHERE thread_id = ? AND content IN (?, ?) ORDER BY created_at",
            (thread_id, "setup failure input", "next queued input"),
        ).fetchall() == [
            ("setup failure input", "failed"),
            ("next queued input", "delivered"),
        ]
    post_turn.assert_awaited_once()


@pytest.mark.asyncio
async def test_event_task_cancellation_emits_cancelled_once(
    isolated_db,
    monkeypatch,
    tmp_path,
):
    from infra.send_service import send_thread_input

    thread_id = "event-cancelled"
    thread_row = _seed_event_thread_row(thread_id)
    product_events: list[tuple] = []

    async def _run_turn(*, on_provider_response, **_kwargs):
        await on_provider_response()
        raise asyncio.CancelledError

    post_turn = AsyncMock()
    _install_event_engine(
        monkeypatch,
        tmp_path,
        run_turn=_run_turn,
        product_events=product_events,
        post_turn=post_turn,
    )

    result = await send_thread_input(
        thread_id,
        thread_row,
        "event content",
        role="system",
        metadata={"type": "context_event"},
        native_mode="event",
        target=_native_target(),
    )
    assert result.turn_task is not None
    with pytest.raises(asyncio.CancelledError):
        await result.turn_task

    assert _turn_status(thread_id) == "cancelled"
    assert [event[1] for event in product_events] == ["started", "cancelled"]
    post_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_preadmitted_setup_cancellation_terminalizes_and_admits_successor(
    isolated_db, monkeypatch, tmp_path,
):
    from infra import send_dispatch
    from infra.send_service import send_thread_input

    thread_id = "event-setup-cancelled"
    thread_row = _seed_event_thread_row(thread_id)
    setup_started = asyncio.Event()
    post_turn = AsyncMock()

    async def run_turn(*, on_provider_response, on_step, **_kwargs):
        await on_provider_response()
        await on_step("text", {"content": "next input completed"})
        await on_step("turn_complete", {"context_usage": None})

    ws_turn = _install_event_engine(
        monkeypatch, tmp_path, run_turn=run_turn, product_events=[], post_turn=post_turn,
    )
    original_setup = ws_turn._setup_turn
    turn_tasks = []
    original_create_task = asyncio.create_task

    async def paused_setup(*_args, **_kwargs):
        setup_started.set()
        await asyncio.Event().wait()

    def capture_native_turn(coro, *args, **kwargs):
        task = original_create_task(coro, *args, **kwargs)
        if getattr(coro, "cr_code", None) and coro.cr_code.co_name == "execute_turn":
            turn_tasks.append(task)
        return task

    monkeypatch.setattr(send_dispatch.asyncio, "create_task", capture_native_turn)
    monkeypatch.setattr(ws_turn, "_setup_turn", paused_setup)
    send_task = original_create_task(send_thread_input(
        thread_id, thread_row, "cancelled during setup", role="system",
        metadata={"type": "context_event"}, native_mode="event", target=_native_target(),
    ))

    await asyncio.wait_for(setup_started.wait(), timeout=2)
    assert len(turn_tasks) == 1
    turn_tasks[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await send_task
    assert _turn_status(thread_id) == "cancelled"

    monkeypatch.setattr(ws_turn, "_setup_turn", original_setup)
    successor = await send_thread_input(
        thread_id, thread_row, "next queued input", role="system",
        metadata={"type": "context_event"}, native_mode="event", target=_native_target(),
    )

    assert successor.turn_task is not None
    assert await successor.turn_task is False
    assert _turn_status(thread_id) == "completed"
    with pytest.raises(asyncio.CancelledError):
        await turn_tasks[0]
    post_turn.assert_awaited_once()


@pytest.mark.asyncio
async def test_preadmitted_bind_cancellation_releases_runtime(
    isolated_db, monkeypatch, tmp_path,
):
    from core import turn_core
    from infra import send_dispatch, state
    from infra.send_service import send_thread_input

    thread_id = "event-bind-cancelled"
    thread_row = _seed_event_thread_row(thread_id)
    bound = asyncio.Event()
    original_run_in_db = turn_core.run_in_db
    original_create_task = asyncio.create_task
    turn_tasks = []
    post_turn = AsyncMock()

    async def run_turn(*, on_provider_response, on_step, **_kwargs):
        await on_provider_response()
        await on_step("text", {"content": "successor"})
        await on_step("turn_complete", {"context_usage": None})

    _install_event_engine(monkeypatch, tmp_path, run_turn=run_turn, product_events=[], post_turn=post_turn)

    async def paused_run_in_db(fn, *args, **kwargs):
        if fn is turn_core._require_open_turn:
            bound.set()
            await asyncio.Event().wait()
        return await original_run_in_db(fn, *args, **kwargs)

    def capture_native_turn(coro, *args, **kwargs):
        task = original_create_task(coro, *args, **kwargs)
        if getattr(coro, "cr_code", None) and coro.cr_code.co_name == "execute_turn":
            turn_tasks.append(task)
        return task

    monkeypatch.setattr(turn_core, "run_in_db", paused_run_in_db)
    monkeypatch.setattr(send_dispatch.asyncio, "create_task", capture_native_turn)
    send_task = original_create_task(send_thread_input(
        thread_id, thread_row, "cancel during bind", role="system",
        metadata={"type": "context_event"}, native_mode="event", target=_native_target(),
    ))

    await asyncio.wait_for(bound.wait(), timeout=2)
    turn_tasks[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await send_task
    assert thread_id not in state._active_turns
    assert thread_id not in state._active_turn_tasks

    monkeypatch.setattr(turn_core, "run_in_db", original_run_in_db)
    successor = await send_thread_input(
        thread_id, thread_row, "next queued input", role="system",
        metadata={"type": "context_event"}, native_mode="event", target=_native_target(),
    )
    assert await successor.turn_task is False
    with pytest.raises(asyncio.CancelledError):
        await turn_tasks[0]


@pytest.mark.asyncio
async def test_preadmitted_setup_cancellation_waits_for_shielded_settlement(monkeypatch):
    from types import SimpleNamespace

    from core.turn_core import TurnIdentity
    from ws import turn as ws_turn

    setup_started = asyncio.Event()
    settlement_started = asyncio.Event()
    settlement_release = asyncio.Event()

    async def paused_setup(*_args, **_kwargs):
        setup_started.set()
        await asyncio.Event().wait()

    async def delayed_settlement(*_args, **_kwargs):
        settlement_started.set()
        await settlement_release.wait()

    request = SimpleNamespace(
        thread_id="shielded-setup-cancellation",
        content="input",
        attachments=[],
        target=None,
        media_projection=None,
        turn_id="shielded-setup-cancellation",
        ingress="event",
        reasoning_effort=None,
        broadcast=None,
        step_metadata=None,
        preadmitted_turn=TurnIdentity("shielded-setup-cancellation", 1),
        provider_acceptance=None,
        start_ack=None,
    )
    monkeypatch.setattr(ws_turn, "_setup_turn", paused_setup)
    monkeypatch.setattr(ws_turn, "settle_preadmitted_failure", delayed_settlement)
    task = asyncio.create_task(ws_turn._execute_turn(request))

    await asyncio.wait_for(setup_started.wait(), timeout=0.2)
    task.cancel()
    await asyncio.wait_for(settlement_started.wait(), timeout=0.2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    settlement_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_site", ["model", "bind"])
async def test_preadmitted_setup_failure_completes_futures_after_receipt_failure(
    monkeypatch,
    failure_site,
):
    from types import SimpleNamespace

    from core.turn_core import TurnIdentity, TurnNotPreadmittedError
    from llm.resolution import ModelUnavailableError
    from ws import turn as ws_turn
    from ws import turn_helpers

    identity = TurnIdentity(f"receipt-before-terminal-{failure_site}", 1)
    receipt_error = sqlite3.OperationalError("receipt settlement failed")
    events = []
    loop = asyncio.get_running_loop()
    provider_acceptance = loop.create_future()
    start_ack = loop.create_future()
    _install_fake_native_context_agent(monkeypatch)
    monkeypatch.setattr(ws_turn, "make_provider_params", lambda _model: {})
    if failure_site == "model":
        error = ModelUnavailableError("anthropic/test", "not available")

        async def fail_target(*_args, **_kwargs):
            raise error

        monkeypatch.setattr(ws_turn, "validate_native_target", fail_target)
    else:
        error = TurnNotPreadmittedError("bind failed")

        async def fail_bind(*_args, **_kwargs):
            raise error

        monkeypatch.setattr(ws_turn, "bind_preadmitted_turn", fail_bind)

    async def settle_delivery_terminal(*_args):
        events.append("receipt")
        raise receipt_error

    async def settle_owned(*_args, **_kwargs):
        events.append("terminal")

    monkeypatch.setattr(turn_helpers, "settle_owned_turn_terminal", settle_owned)
    request = SimpleNamespace(
        thread_id=identity.turn_id,
        content="input",
        attachments=[],
        target=_native_target(),
        media_projection=None,
        turn_id=identity.turn_id,
        ingress="event",
        reasoning_effort=None,
        broadcast=AsyncMock(),
        step_metadata=None,
        preadmitted_turn=identity,
        provider_acceptance=provider_acceptance,
        start_ack=start_ack,
        settle_delivery_terminal=settle_delivery_terminal,
        load_for=(),
    )

    with pytest.raises(sqlite3.OperationalError, match="receipt settlement failed") as raised:
        await ws_turn._execute_turn(request)

    assert raised.value is receipt_error
    assert events == ["receipt", "terminal"]
    assert provider_acceptance.result() is False
    with pytest.raises(type(error), match=str(error)):
        start_ack.result()


@pytest.mark.asyncio
async def test_failed_admission_releases_before_driving_successor(monkeypatch):
    from types import SimpleNamespace

    from core import turn_core
    from core.native_turn_control import TurnCancelEvent
    from core.turn_core import TurnIdentity, TurnTerminalResult
    from ws.turn import _finish_failed_admission

    identity = TurnIdentity("release-before-successor", 1)
    cancel_event = TurnCancelEvent()
    unbound, driven = [], []
    settlement = SimpleNamespace(result=TurnTerminalResult.APPLIED)

    async def commit(*_args, **_kwargs):
        return settlement

    async def drive(actual_settlement):
        assert unbound == [identity]
        driven.append(actual_settlement)

    monkeypatch.setattr(turn_core, "commit_turn_terminal", commit)
    monkeypatch.setattr(turn_core, "drive_turn_settlement", drive)
    original = ValueError("admission failed")
    prepared = SimpleNamespace(identity=identity, cancel_event=cancel_event, unbind=unbound.append)
    request = SimpleNamespace(
        settle_delivery_terminal=None,
        provider_acceptance=None,
        start_ack=None,
    )

    await _finish_failed_admission(prepared, request, original)

    assert driven == [settlement]
    assert unbound == [identity, identity]
    assert cancel_event.completion.exception() is original


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_admission_completes_cleanup_when_receipt_settlement_fails(cancelled):
    from types import SimpleNamespace

    from core.native_turn_control import TurnCancelEvent
    from core.turn_core import TurnIdentity
    from ws.turn import _finish_failed_admission

    identity = TurnIdentity("receipt-failure-turn", 1)
    cancel_event = TurnCancelEvent()
    unbound, receipts = [], []
    original = asyncio.CancelledError() if cancelled else ValueError("admission failed")
    receipt_error = sqlite3.OperationalError("receipt settlement failed")

    async def settle(outcome, detail):
        receipts.append((outcome, detail))
        raise receipt_error

    prepared = SimpleNamespace(identity=identity, cancel_event=cancel_event, unbind=unbound.append)
    request = SimpleNamespace(
        settle_delivery_terminal=settle,
        provider_acceptance=None,
        start_ack=None,
    )
    with pytest.raises(sqlite3.OperationalError, match="receipt settlement failed") as raised:
        await _finish_failed_admission(prepared, request, original)

    assert raised.value.__cause__ is original
    assert receipts == [("cancelled", None) if cancelled else ("error", "admission failed")]
    assert unbound == [identity]
    assert cancel_event.completion.done()
    assert cancel_event.completion.exception() is receipt_error
