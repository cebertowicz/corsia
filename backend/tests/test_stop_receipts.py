from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _clear_live_turn_state():
    from infra import state
    from subagents import cc_runtime, codex_runtime

    state._active_turns.clear()
    state._active_llm_tasks.clear()
    cc_runtime._LIVE.reset()
    codex_runtime._LIVE.reset()
    yield
    state._active_turns.clear()
    state._active_llm_tasks.clear()
    cc_runtime._LIVE.reset()
    codex_runtime._LIVE.reset()


def _active_turn(main_db, turn_id: str, thread_id: str = "main-1"):
    from core.native_turn_control import TurnCancelEvent
    from core.turn_core import persist_turn_start
    from infra import state
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.thread_driver import admit_input

    admission = admit_input(
        thread_id,
        role="user",
        content="Stop test",
        intent="apply_now",
        message_id=f"{turn_id}:input",
        turn_id=turn_id,
        boot_epoch="test-boot",
        admitted_at="t",
        db=main_db,
    )
    assert admission.batch is not None
    attempt_id = f"{turn_id}:write"
    begin_batch_write(
        admission.batch, attempt_id=attempt_id, started_at="t", db=main_db,
    )
    settle_batch_write(attempt_id, outcome="accepted", db=main_db)
    model = main_db.execute(
        "SELECT model FROM threads WHERE id=?", (thread_id,),
    ).fetchone()["model"]
    identity = persist_turn_start(
        turn_id, thread_id, model, "Stop test", db=main_db,
    )
    main_db.commit()
    event = TurnCancelEvent()
    event.bind_turn_identity(identity)
    state._active_turns[thread_id] = event
    return identity, event


def _insert_child_session(main_db, thread_id: str = "child-1"):
    main_db.execute(
        "INSERT INTO threads "
        "(id, user_id, model, created_at, updated_at, spawned_by) "
        "VALUES (?, 'steve', 'test-model', 't', 't', 'main-1')",
        (thread_id,),
    )
    main_db.commit()


async def _settle_stop_work() -> None:
    from ws.ingress import background_tasks

    while background_tasks:
        await asyncio.gather(*tuple(background_tasks), return_exceptions=True)


def _use_fixture_db(monkeypatch, main_db):
    import ws.stop_control as stop_control

    async def _run_in_db(fn, *args, **kwargs):
        return fn(*args, db=main_db, **kwargs)

    monkeypatch.setattr(stop_control, "run_in_db", _run_in_db)


@pytest.mark.asyncio
async def test_stop_returns_stopped_receipt_for_exact_native_turn(main_db, monkeypatch):
    import ws as ws_mod
    from core.turn_core import persist_turn_terminal
    from host_access import processes
    from subagents import cc_runtime, codex_runtime

    identity, event = _active_turn(main_db, "turn-current")
    _use_fixture_db(monkeypatch, main_db)
    monkeypatch.setattr(ws_mod, "_owns_thread", AsyncMock(return_value=True))
    monkeypatch.setattr(cc_runtime, "has_pending_or_live", lambda _sid: False)
    monkeypatch.setattr(codex_runtime, "is_live", lambda _sid: False)
    monkeypatch.setattr(processes.PROCESS_OWNER, "terminate_turn", AsyncMock())
    persist_turn_terminal(
        identity.turn_id, "cancelled", db=main_db,
    )
    main_db.commit()
    event.completion.set_result(True)

    receipt = await ws_mod._do_stop(
        {"id": "steve"}, "main-1", event, None, identity.turn_id,
    )

    assert receipt.payload() == {
        "type": "stop_receipt", "thread_id": "main-1",
        "outcome": "stopped", "turn_id": identity.turn_id,
    }
    assert event.is_set()


@pytest.mark.parametrize(
    ("durable_outcome", "receipt_outcome"),
    [
        ("cancelled", "stopped"),
        ("completed", "already_terminal"),
        ("error", "already_terminal"),
    ],
)
@pytest.mark.asyncio
async def test_runtime_stop_receipt_reflects_exact_durable_terminal_winner(
    main_db,
    monkeypatch,
    durable_outcome,
    receipt_outcome,
):
    import ws as ws_mod
    from core.turn_core import persist_turn_terminal
    from infra import state
    from subagents import cc_runtime, codex_runtime

    identity, _event = _active_turn(main_db, "turn-runtime-winner")
    state._active_turns.pop("main-1", None)
    _use_fixture_db(monkeypatch, main_db)
    monkeypatch.setattr(ws_mod, "_owns_thread", AsyncMock(return_value=True))
    monkeypatch.setattr(cc_runtime, "has_pending_or_live", lambda _sid: False)
    monkeypatch.setattr(codex_runtime, "has_pending_or_live", lambda _sid: True)

    async def stop_after_terminal(*_args, **_kwargs):
        persist_turn_terminal(
            identity.turn_id,
            durable_outcome,
            db=main_db,
        )
        main_db.commit()
        return True

    monkeypatch.setattr(codex_runtime, "stop", stop_after_terminal)

    receipt = await ws_mod._do_stop(
        {"id": "steve"}, "main-1", None, None, identity.turn_id,
    )

    assert receipt.outcome == receipt_outcome
    assert receipt.turn_id == identity.turn_id


@pytest.mark.asyncio
async def test_idle_runtime_handle_after_terminal_reports_already_terminal(
    main_db,
    monkeypatch,
):
    """A reusable handle left after the Turn ended does not reopen that Turn."""
    import ws as ws_mod
    from core.turn_core import persist_turn_terminal
    from infra import state
    from subagents import cc_runtime, codex_runtime

    identity, _event = _active_turn(main_db, "turn-retained-owner")
    state._active_turns.pop("main-1", None)
    persist_turn_terminal(identity.turn_id, "completed", db=main_db)
    main_db.commit()
    _use_fixture_db(monkeypatch, main_db)
    monkeypatch.setattr(ws_mod, "_owns_thread", AsyncMock(return_value=True))
    monkeypatch.setattr(cc_runtime, "has_pending_or_live", lambda _sid: False)
    monkeypatch.setattr(cc_runtime, "is_live", lambda _sid: False)
    monkeypatch.setattr(codex_runtime, "has_pending_or_live", lambda _sid: True)
    monkeypatch.setattr(codex_runtime, "is_live", lambda _sid: True)
    monkeypatch.setattr(codex_runtime, "stop", AsyncMock(return_value=False))

    receipt = await ws_mod._do_stop(
        {"id": "steve"}, "main-1", None, None, identity.turn_id,
    )

    assert receipt.outcome == "already_terminal"
    assert main_db.execute(
        "SELECT status FROM active_turns WHERE id=?", (identity.turn_id,),
    ).fetchone()["status"] == "completed"


@pytest.mark.asyncio
async def test_native_stopped_receipt_waits_for_exact_terminal_settlement(
    main_db, monkeypatch,
):
    import ws as ws_mod
    from core.turn_core import persist_turn_terminal
    from host_access import processes
    from infra import state
    from subagents import cc_runtime, codex_runtime

    identity, event = _active_turn(main_db, "turn-native-settlement")
    _use_fixture_db(monkeypatch, main_db)
    monkeypatch.setattr(ws_mod, "_owns_thread", AsyncMock(return_value=True))
    monkeypatch.setattr(cc_runtime, "has_pending_or_live", lambda _sid: False)
    monkeypatch.setattr(codex_runtime, "is_live", lambda _sid: False)
    monkeypatch.setattr(processes.PROCESS_OWNER, "terminate_turn", AsyncMock())
    cancelled = asyncio.Event()
    release_terminal = asyncio.Event()

    async def _provider_work():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release_terminal.wait()
            persist_turn_terminal(
                identity.turn_id, "cancelled", db=main_db,
            )
            main_db.commit()
            state._active_turns.pop("main-1", None)
            state._active_llm_tasks.pop("main-1", None)
            event.completion.set_result(True)
            raise

    provider_task = asyncio.create_task(_provider_work())
    state._active_llm_tasks["main-1"] = provider_task
    stop_action = asyncio.create_task(ws_mod._do_stop(
        {"id": "steve"}, "main-1", event, provider_task, identity.turn_id,
    ))
    try:
        await cancelled.wait()
        returned_before_terminal = stop_action.done()
        release_terminal.set()
        receipt = await stop_action
        await asyncio.gather(provider_task, return_exceptions=True)

        row = main_db.execute(
            "SELECT status FROM active_turns WHERE id=?", (identity.turn_id,),
        ).fetchone()
        assert returned_before_terminal is False
        assert receipt.outcome == "stopped"
        assert row["status"] == "cancelled"
        assert "main-1" not in state._active_turns
        assert "main-1" not in state._active_llm_tasks
    finally:
        release_terminal.set()
        if not provider_task.done():
            provider_task.cancel()
        await asyncio.gather(provider_task, return_exceptions=True)
        if not stop_action.done():
            stop_action.cancel()
        await asyncio.gather(stop_action, return_exceptions=True)


@pytest.mark.asyncio
async def test_native_stop_never_cancels_warm_registered_by_successor(
    main_db, monkeypatch,
):
    import ws as ws_mod
    from core.turn_core import persist_turn_terminal
    from host_access import processes
    from infra import state
    from subagents import cc_runtime, codex_runtime

    identity, event = _active_turn(main_db, "turn-native-before-successor")
    _use_fixture_db(monkeypatch, main_db)
    monkeypatch.setattr(ws_mod, "_owns_thread", AsyncMock(return_value=True))
    monkeypatch.setattr(cc_runtime, "has_pending_or_live", lambda _sid: False)
    monkeypatch.setattr(codex_runtime, "has_pending_or_live", lambda _sid: False)
    monkeypatch.setattr(processes.PROCESS_OWNER, "terminate_turn", AsyncMock())
    successor_warm = None

    async def _provider_work():
        nonlocal successor_warm
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            persist_turn_terminal(
                identity.turn_id, "cancelled", db=main_db,
            )
            main_db.commit()
            state._active_turns.pop("main-1", None)
            state._active_llm_tasks.pop("main-1", None)
            successor_warm = asyncio.create_task(asyncio.Event().wait())
            state._active_warm_tasks["main-1"] = successor_warm
            event.completion.set_result(True)
            raise

    provider_task = asyncio.create_task(_provider_work())
    state._active_llm_tasks["main-1"] = provider_task
    await asyncio.sleep(0)
    try:
        receipt = await ws_mod._do_stop(
            {"id": "steve"}, "main-1", event, provider_task, identity.turn_id,
        )

        assert receipt.outcome == "stopped"
        assert successor_warm is not None
        assert not successor_warm.done()
        assert state._active_warm_tasks["main-1"] is successor_warm
    finally:
        await asyncio.gather(provider_task, return_exceptions=True)
        if successor_warm is not None and not successor_warm.done():
            successor_warm.cancel()
            await asyncio.gather(successor_warm, return_exceptions=True)
        state._active_warm_tasks.pop("main-1", None)


@pytest.mark.asyncio
async def test_explicit_stop_cancels_and_joins_exact_thread_warm(
    main_db, monkeypatch,
):
    import asyncio
    import ws as ws_mod
    from core.turn_core import persist_turn_terminal
    from host_access import processes
    from infra import state
    from subagents import cc_runtime, codex_runtime

    identity, event = _active_turn(main_db, "turn-with-warm")
    _use_fixture_db(monkeypatch, main_db)
    monkeypatch.setattr(ws_mod, "_owns_thread", AsyncMock(return_value=True))
    monkeypatch.setattr(cc_runtime, "has_pending_or_live", lambda _sid: False)
    monkeypatch.setattr(codex_runtime, "is_live", lambda _sid: False)
    monkeypatch.setattr(processes.PROCESS_OWNER, "terminate_turn", AsyncMock())
    persist_turn_terminal(
        identity.turn_id, "cancelled", db=main_db,
    )
    main_db.commit()
    event.completion.set_result(True)
    warm_started = asyncio.Event()
    warm_cancelled = asyncio.Event()
    warm_joined = asyncio.Event()

    async def _warm():
        warm_started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            warm_cancelled.set()
            await asyncio.sleep(0)
            warm_joined.set()
            raise

    warm_task = asyncio.create_task(_warm())
    state._active_warm_tasks["main-1"] = warm_task
    await warm_started.wait()

    try:
        receipt = await ws_mod._do_stop(
            {"id": "steve"}, "main-1", event, None, identity.turn_id,
        )

        assert receipt.outcome == "stopped"
        assert warm_cancelled.is_set()
        assert warm_joined.is_set()
        assert warm_task.done()
        assert "main-1" not in state._active_warm_tasks
    finally:
        if not warm_task.done():
            warm_task.cancel()
            await asyncio.gather(warm_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stale_stop_receipt_is_observational_and_never_signals_successor(
    main_db, monkeypatch,
):
    import ws as ws_mod
    from subagents import cc_runtime, codex_runtime

    _identity, successor = _active_turn(main_db, "turn-successor")
    _use_fixture_db(monkeypatch, main_db)
    monkeypatch.setattr(ws_mod, "_owns_thread", AsyncMock(return_value=True))
    monkeypatch.setattr(cc_runtime, "has_pending_or_live", lambda _sid: False)
    monkeypatch.setattr(codex_runtime, "is_live", lambda _sid: False)

    receipt = await ws_mod._do_stop(
        {"id": "steve"}, "main-1", successor, None, "turn-stale",
    )

    assert receipt.payload() == {
        "type": "stop_receipt", "thread_id": "main-1",
        "outcome": "stale_active", "turn_id": "turn-stale",
    }
    assert not successor.is_set()


@pytest.mark.asyncio
async def test_stop_dispatch_sends_requester_visible_no_active_receipt():
    import ws as ws_mod

    ws = type("WebSocket", (), {"send_json": AsyncMock()})()
    ws_mod._handle_stop({"id": "steve"}, "main-1", "", ws=ws)
    for _ in range(10):
        await __import__("asyncio").sleep(0)

    ws.send_json.assert_awaited_once_with({
        "type": "stop_receipt", "thread_id": "main-1", "outcome": "no_active_turn",
    })


@pytest.mark.asyncio
async def test_subagent_stop_cancels_and_joins_exact_native_turn(
    main_db, monkeypatch,
):
    from core.turn_core import persist_turn_terminal
    from host_access import processes
    from infra import state
    from subagents import cc_runtime, codex_runtime
    from ws.subagent_control import handle_subagent_stop

    _insert_child_session(main_db)
    identity, event = _active_turn(main_db, "turn-child", "child-1")
    _use_fixture_db(monkeypatch, main_db)
    monkeypatch.setattr(cc_runtime, "has_pending_or_live", lambda _sid: False)
    monkeypatch.setattr(codex_runtime, "has_pending_or_live", lambda _sid: False)
    monkeypatch.setattr(processes.PROCESS_OWNER, "terminate_turn", AsyncMock())
    cancelled = asyncio.Event()
    joined = asyncio.Event()

    async def _provider_work():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            persist_turn_terminal(
                identity.turn_id, "cancelled", db=main_db,
            )
            main_db.commit()
            state._active_turns.pop("child-1", None)
            state._active_llm_tasks.pop("child-1", None)
            joined.set()
            event.completion.set_result(True)
            raise

    provider_task = asyncio.create_task(_provider_work())
    state._active_llm_tasks["child-1"] = provider_task
    await asyncio.sleep(0)
    ws = type("WebSocket", (), {"send_json": AsyncMock()})()
    owns = AsyncMock(return_value={"id": "child-1"})

    try:
        await handle_subagent_stop(
            ws, {"id": "steve"}, "child-1", identity.turn_id,
            owned_thread_fn=owns,
        )
        row = main_db.execute(
            "SELECT status FROM active_turns WHERE id=?", (identity.turn_id,),
        ).fetchone()

        assert cancelled.is_set()
        assert joined.is_set()
        assert provider_task.done()
        assert row["status"] == "cancelled"
        ws.send_json.assert_awaited_once_with({
            "type": "stop_receipt", "thread_id": "child-1",
            "outcome": "stopped", "turn_id": identity.turn_id,
        })
    finally:
        if not provider_task.done():
            provider_task.cancel()
        await asyncio.gather(provider_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_subagent_stop_never_signals_successor_after_ingress(
    main_db, monkeypatch,
):
    from core.turn_core import TurnIdentity
    from core.native_turn_control import TurnCancelEvent
    from infra import state
    from subagents import cc_runtime, codex_runtime
    from ws.subagent_control import handle_subagent_stop

    _insert_child_session(main_db)
    identity, source = _active_turn(main_db, "turn-child", "child-1")
    successor = TurnCancelEvent()
    successor.bind_turn_identity(TurnIdentity("turn-successor", 2))
    _use_fixture_db(monkeypatch, main_db)
    monkeypatch.setattr(cc_runtime, "has_pending_or_live", lambda _sid: False)
    monkeypatch.setattr(codex_runtime, "has_pending_or_live", lambda _sid: False)
    ownership_started = asyncio.Event()
    release_ownership = asyncio.Event()

    async def _owns(_user, _sid):
        ownership_started.set()
        await release_ownership.wait()
        return {"id": "child-1"}

    ws = type("WebSocket", (), {"send_json": AsyncMock()})()
    stop_action = asyncio.create_task(handle_subagent_stop(
        ws, {"id": "steve"}, "child-1", identity.turn_id,
        owned_thread_fn=_owns,
    ))
    await ownership_started.wait()
    state._active_turns["child-1"] = successor
    release_ownership.set()
    await stop_action
    await _settle_stop_work()

    assert not source.is_set()
    assert not successor.is_set()
    ws.send_json.assert_awaited_once_with({
        "type": "stop_receipt", "thread_id": "child-1",
        "outcome": "stopped", "turn_id": identity.turn_id,
    })
    assert main_db.execute(
        "SELECT status FROM active_turns WHERE id=?", (identity.turn_id,),
    ).fetchone()["status"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("thread_id", "turn_id"),
    [("child-1", None), ("", "turn-missing")],
)
async def test_subagent_stop_malformed_or_missing_owner_returns_no_active_receipt(
    thread_id, turn_id,
):
    from ws.subagent_control import handle_subagent_stop

    ws = type("WebSocket", (), {"send_json": AsyncMock()})()
    await handle_subagent_stop(
        ws, {"id": "steve"}, thread_id, turn_id,
        owned_thread_fn=AsyncMock(return_value=None),
    )

    ws.send_json.assert_awaited_once_with({
        "type": "stop_receipt", "thread_id": thread_id, "outcome": "no_active_turn",
    })


@pytest.mark.asyncio
async def test_stop_action_failure_emits_requester_scoped_ws_error():
    import ws as ws_mod

    async def fail_stop():
        raise RuntimeError("provider shutdown failed")

    ws = type("WebSocket", (), {"send_json": AsyncMock()})()
    await ws_mod._send_stop_receipt(
        ws, "main-1", fail_stop(), "turn-1",
    )

    ws.send_json.assert_awaited_once_with({
        "type": "error",
        "thread_id": "main-1",
        "error_type": "stop_failed",
        "turn_id": "turn-1",
        "message": "Stop failed: provider shutdown failed",
    })


@pytest.mark.asyncio
async def test_completed_stop_ignores_a_requester_socket_that_already_closed():
    from fastapi import WebSocketDisconnect

    import ws as ws_mod

    ws = type("WebSocket", (), {
        "send_json": AsyncMock(side_effect=WebSocketDisconnect()),
    })()

    await ws_mod._send_stop_receipt(
        ws,
        "main-1",
        ws_mod._immediate_stop_receipt("main-1"),
    )

    ws.send_json.assert_awaited_once()


@pytest.mark.asyncio
async def test_runtime_stop_that_leaves_turn_active_still_ends_it(
    main_db, monkeypatch,
):
    """A runtime that cannot end the exact Turn never becomes Stop's result."""
    import ws as ws_mod
    from infra import state
    from subagents import cc_runtime, codex_runtime

    identity, _event = _active_turn(main_db, "turn-stop-failed")
    state._active_turns.pop("main-1", None)
    _use_fixture_db(monkeypatch, main_db)
    monkeypatch.setattr(ws_mod, "_owns_thread", AsyncMock(return_value=True))
    monkeypatch.setattr(cc_runtime, "has_pending_or_live", lambda _sid: False)
    monkeypatch.setattr(codex_runtime, "has_pending_or_live", lambda _sid: True)
    monkeypatch.setattr(codex_runtime, "stop", AsyncMock(return_value=False))

    receipt = await ws_mod._do_stop(
        {"id": "steve"}, "main-1", None, None, identity.turn_id,
    )
    await _settle_stop_work()

    assert (receipt.outcome, receipt.turn_id) == ("stopped", identity.turn_id)
    assert main_db.execute(
        "SELECT status FROM active_turns WHERE id=?", (identity.turn_id,),
    ).fetchone()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_subagent_stop_failure_emits_requester_scoped_ws_error(monkeypatch):
    import ws.stop_control as stop_control
    from ws.subagent_control import handle_subagent_stop

    async def fail_stop(*_args, **_kwargs):
        raise RuntimeError("subagent provider shutdown failed")

    monkeypatch.setattr(stop_control, "do_stop", fail_stop)
    ws = type("WebSocket", (), {"send_json": AsyncMock()})()

    await handle_subagent_stop(
        ws, {"id": "steve"}, "child-1", "turn-child",
        owned_thread_fn=AsyncMock(return_value={"id": "child-1"}),
    )

    ws.send_json.assert_awaited_once_with({
        "type": "error",
        "thread_id": "child-1",
        "error_type": "stop_failed",
        "turn_id": "turn-child",
        "message": "Stop failed: subagent provider shutdown failed",
    })


@pytest.mark.asyncio
async def test_stop_commit_cancels_the_plan_liveness_wake(main_db):
    """The one Stop-commit seam cancels an armed wake atomically with the
    durable cancel it applies -- a left-armed wake would wait for progress
    that a stopped Turn can never produce."""
    from automation import plan_liveness
    from plans import boundary, service
    from ws.stop_control import _commit_stop_terminal

    identity, _event = _active_turn(main_db, "turn-stop-wake")
    plan = service.ensure_plan("thread", "main-1", db=main_db)
    node = service.add_node(
        plan.id, kind="task", title="Continue the selected work",
        acceptance_criteria="The selected work finishes", db=main_db,
    )
    boundary.mutate(
        plan.id, "activate",
        {"node_id": node.id, "expected_revision": node.revision},
        actor=boundary.PlanActor("main-1", "steve", None),
        provenance="test.stop_receipts", mutation_id="activate", db=main_db,
    )
    wake = plan_liveness.on_turn_complete("main-1", db=main_db)
    main_db.commit()
    assert wake is not None and wake.status == "armed"

    status, applied = _commit_stop_terminal(
        "steve", "main-1", identity.turn_id, db=main_db,
    )

    assert status == "cancelled"
    assert applied is not None
    row = main_db.execute(
        "SELECT status FROM plan_liveness_wakes WHERE thread_id=?", ("main-1",),
    ).fetchone()
    assert row["status"] == "cancelled"


def _queue_message(main_db, content: str, *, role="user", continuation=None) -> str:
    import uuid

    from infra.thread_driver import admit_input

    message_id = uuid.uuid4().hex
    admit_input(
        "main-1", role=role, content=content, intent="apply_now",
        message_id=message_id, turn_id=uuid.uuid4().hex, boot_epoch="test-boot",
        admitted_at="t", continuation=continuation, db=main_db,
    )
    main_db.commit()
    return message_id


def _arm_work_and_queue_liveness_wake(main_db) -> str:
    """Open plan work, an armed due wake row, and its wake queued in the FIFO."""
    from datetime import datetime, timedelta, timezone

    from automation import plan_liveness
    from automation.continuation.execution import event_for
    from automation.continuation.sources.liveness import LivenessSource
    from automation.continuation.validity import condition_for_event
    from plans import boundary, service

    plan = service.ensure_plan("thread", "main-1", db=main_db)
    node = service.add_node(
        plan.id, kind="task", title="Continue the selected work",
        acceptance_criteria="The selected work finishes", db=main_db,
    )
    boundary.mutate(
        plan.id, "activate",
        {"node_id": node.id, "expected_revision": node.revision},
        actor=boundary.PlanActor("main-1", "steve", None),
        provenance="test.stop_receipts", mutation_id="activate", db=main_db,
    )
    plan_liveness.on_turn_complete("main-1", db=main_db)
    main_db.execute(
        "UPDATE plan_liveness_wakes SET deadline=? WHERE thread_id='main-1'",
        ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
    )
    main_db.commit()
    due = datetime.now(timezone.utc).isoformat()
    request = LivenessSource().due(due, db=main_db)[0]
    _event, _summary, details = event_for(request)
    condition = condition_for_event("plan.liveness_wake", details)
    assert condition is not None
    return _queue_message(
        main_db, "Actionable structured plan work remains.",
        role="system", continuation=condition,
    )


def _wake_state(main_db, wake_id: str) -> tuple[str, str | None, str | None]:
    row = main_db.execute(
        "SELECT delivery_state,delivery_error,active_turn_id FROM messages WHERE id=?",
        (wake_id,),
    ).fetchone()
    return row["delivery_state"], row["delivery_error"], row["active_turn_id"]


def _running_turns(main_db) -> int:
    return main_db.execute(
        "SELECT COUNT(*) AS n FROM active_turns WHERE thread_id='main-1' "
        "AND status='running'",
    ).fetchone()["n"]


@pytest.mark.asyncio
async def test_one_stop_leaves_a_queued_liveness_wake_unclaimed(main_db):
    """Stop means stop: the wake queued behind the Turn is dropped, not run."""
    from ws.stop_control import _commit_stop_terminal

    identity, _event = _active_turn(main_db, "turn-stop-queued-wake")
    wake_id = _arm_work_and_queue_liveness_wake(main_db)

    status, applied = _commit_stop_terminal(
        "steve", "main-1", identity.turn_id, db=main_db,
    )

    assert status == "cancelled" and applied is not None
    assert applied[1].successor is None
    assert _running_turns(main_db) == 0
    assert _wake_state(main_db, wake_id) == ("cancelled", "stale_plan_wake", None)
    row = main_db.execute(
        "SELECT status FROM plan_liveness_wakes WHERE thread_id='main-1'",
    ).fetchone()
    assert row["status"] == "cancelled"


@pytest.mark.asyncio
async def test_stop_drops_a_liveness_wake_but_runs_the_person_behind_it(main_db):
    from ws.stop_control import _commit_stop_terminal

    identity, _event = _active_turn(main_db, "turn-stop-fifo")
    wake_id = _arm_work_and_queue_liveness_wake(main_db)
    person_id = _queue_message(main_db, "Please carry on")

    _status, applied = _commit_stop_terminal(
        "steve", "main-1", identity.turn_id, db=main_db,
    )

    assert applied is not None and applied[1].successor is not None
    assert _wake_state(main_db, wake_id)[:2] == ("cancelled", "stale_plan_wake")
    state, _error, turn = _wake_state(main_db, person_id)
    assert state == "queued" and turn is not None
    assert _running_turns(main_db) == 1


@pytest.mark.asyncio
async def test_stop_runs_a_queued_person_message_with_no_wake(main_db):
    from ws.stop_control import _commit_stop_terminal

    identity, _event = _active_turn(main_db, "turn-stop-person")
    person_id = _queue_message(main_db, "Next thing")

    _status, applied = _commit_stop_terminal(
        "steve", "main-1", identity.turn_id, db=main_db,
    )

    assert applied is not None and applied[1].successor is not None
    assert _wake_state(main_db, person_id)[2] is not None


@pytest.mark.asyncio
async def test_plan_limit_stop_keeps_a_queued_liveness_wake(main_db):
    from core.stop_causes import PlanLimitStop
    from ws.stop_control import _commit_stop_terminal

    identity, _event = _active_turn(main_db, "turn-limit-wake")
    wake_id = _arm_work_and_queue_liveness_wake(main_db)

    _commit_stop_terminal(
        "steve", "main-1", identity.turn_id, db=main_db,
        cause=PlanLimitStop(),
    )

    assert _wake_state(main_db, wake_id)[0] == "queued"
    row = main_db.execute(
        "SELECT status FROM plan_liveness_wakes WHERE thread_id='main-1'",
    ).fetchone()
    assert row["status"] == "armed"


@pytest.mark.asyncio
async def test_a_stop_that_does_not_apply_leaves_the_wake_untouched(main_db):
    from ws.stop_control import _commit_stop_terminal

    identity, _event = _active_turn(main_db, "turn-stop-noop")
    wake_id = _arm_work_and_queue_liveness_wake(main_db)
    main_db.execute(
        "UPDATE active_turns SET status='completed' WHERE id=?", (identity.turn_id,),
    )
    main_db.commit()

    status, applied = _commit_stop_terminal(
        "steve", "main-1", identity.turn_id, db=main_db,
    )

    assert status == "completed" and applied is None
    assert _wake_state(main_db, wake_id)[0] == "queued"
    row = main_db.execute(
        "SELECT status FROM plan_liveness_wakes WHERE thread_id='main-1'",
    ).fetchone()
    assert row["status"] == "armed"


@pytest.mark.asyncio
async def test_a_stop_held_by_a_restart_still_never_runs_its_queued_wake(
    main_db, monkeypatch,
):
    """A held claim leaves the wake queued; the re-armed row must not revive it."""
    import uuid

    from automation import plan_liveness, self_restart
    from infra.thread_inputs import start_idle_turn
    from ws.stop_control import _commit_stop_terminal

    identity, _event = _active_turn(main_db, "turn-stop-held")
    wake_id = _arm_work_and_queue_liveness_wake(main_db)
    monkeypatch.setattr(self_restart, "restart_holds_new_work", lambda db=None: True)

    _status, applied = _commit_stop_terminal(
        "steve", "main-1", identity.turn_id, db=main_db,
    )

    assert applied is not None and applied[1].successor is None
    monkeypatch.undo()
    plan_liveness.on_turn_complete("main-1", db=main_db)
    main_db.commit()
    assert start_idle_turn(
        "main-1", turn_id=uuid.uuid4().hex, boot_epoch="test-boot",
        started_at="t", db=main_db,
    ) is None
    assert _running_turns(main_db) == 0
    assert _wake_state(main_db, wake_id) == ("cancelled", "stale_plan_wake", None)
