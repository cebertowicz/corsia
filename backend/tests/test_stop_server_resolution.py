from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

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


async def _settle_background_work() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


def _active_turn(main_db, turn_id: str, *, bind_native: bool = True):
    from core.native_turn_control import TurnCancelEvent
    from core.turn_core import persist_turn_start
    from infra import state
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.thread_driver import admit_input

    admission = admit_input(
        "main-1",
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
        "SELECT model FROM threads WHERE id='main-1'",
    ).fetchone()["model"]
    identity = persist_turn_start(
        turn_id,
        "main-1",
        model,
        "Stop test",
        db=main_db,
    )
    main_db.commit()
    event = TurnCancelEvent()
    event.bind_turn_identity(identity)
    if bind_native:
        state._active_turns["main-1"] = event
    return identity, event


def _use_fixture_db_for_stop_resolution(monkeypatch, main_db) -> None:
    import ws.stop_control as stop_control

    async def _run_in_db(fn, *args, **kwargs):
        return fn(*args, db=main_db, **kwargs)

    monkeypatch.setattr(stop_control, "run_in_db", _run_in_db)


@pytest.mark.asyncio
async def test_stop_without_client_turn_id_resolves_current_native_turn(
    main_db, monkeypatch,
):
    import ws as ws_mod
    from host_access import processes
    from subagents import cc_runtime, codex_runtime

    _identity, event = _active_turn(main_db, "turn-current")
    _use_fixture_db_for_stop_resolution(monkeypatch, main_db)
    monkeypatch.setattr(ws_mod, "_owns_thread", AsyncMock(return_value=True))
    monkeypatch.setattr(cc_runtime, "has_pending_or_live", lambda _sid: False)
    monkeypatch.setattr(codex_runtime, "has_pending_or_live", lambda _sid: False)
    monkeypatch.setattr(
        processes.PROCESS_OWNER, "terminate_turn", AsyncMock(return_value=None),
    )

    ws_mod._handle_stop({"id": "steve"}, "main-1")
    await _settle_background_work()

    assert event.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["cc", "codex"])
async def test_stop_without_client_turn_id_routes_resolved_identity_to_live_harness(
    main_db, monkeypatch, harness,
):
    import ws as ws_mod
    from subagents import cc_runtime, codex_runtime

    from core.turn_core import persist_turn_terminal

    identity, _event = _active_turn(main_db, "turn-current", bind_native=False)
    _use_fixture_db_for_stop_resolution(monkeypatch, main_db)
    monkeypatch.setattr(ws_mod, "_owns_thread", AsyncMock(return_value=True))

    async def end_turn(*_args, **_kwargs) -> bool:
        persist_turn_terminal(identity.turn_id, "cancelled", db=main_db)
        main_db.commit()
        return True

    cc_stop = AsyncMock(side_effect=end_turn)
    codex_stop = AsyncMock(side_effect=end_turn)
    monkeypatch.setattr(cc_runtime, "has_pending_or_live", lambda _sid: harness == "cc")
    monkeypatch.setattr(
        codex_runtime, "has_pending_or_live", lambda _sid: harness == "codex",
    )
    monkeypatch.setattr(cc_runtime, "stop", cc_stop)
    monkeypatch.setattr(codex_runtime, "stop", codex_stop)

    ws_mod._handle_stop({"id": "steve"}, "main-1")
    await _settle_background_work()

    expected = {"expected_turn_id": identity.turn_id, "broadcast": ANY}
    if harness == "cc":
        cc_stop.assert_awaited_once_with("main-1", **expected)
        codex_stop.assert_not_awaited()
    else:
        codex_stop.assert_awaited_once_with("main-1", **expected)
        cc_stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_explicit_stop_cannot_cancel_a_successor_turn(main_db, monkeypatch):
    import ws as ws_mod
    from subagents import cc_runtime, codex_runtime

    _identity, successor = _active_turn(main_db, "turn-successor")
    _use_fixture_db_for_stop_resolution(monkeypatch, main_db)
    monkeypatch.setattr(ws_mod, "_owns_thread", AsyncMock(return_value=True))
    monkeypatch.setattr(cc_runtime, "has_pending_or_live", lambda _sid: False)
    monkeypatch.setattr(codex_runtime, "has_pending_or_live", lambda _sid: False)

    ws_mod._handle_stop({"id": "steve"}, "main-1", "turn-stale")
    await _settle_background_work()

    assert not successor.is_set()


@pytest.mark.asyncio
async def test_malformed_explicit_stop_does_not_resolve_to_a_current_turn(
    main_db, monkeypatch,
):
    import ws as ws_mod
    from subagents import cc_runtime, codex_runtime

    _identity, current = _active_turn(main_db, "turn-current")
    _use_fixture_db_for_stop_resolution(monkeypatch, main_db)
    monkeypatch.setattr(ws_mod, "_owns_thread", AsyncMock(return_value=True))
    monkeypatch.setattr(cc_runtime, "has_pending_or_live", lambda _sid: False)
    monkeypatch.setattr(codex_runtime, "has_pending_or_live", lambda _sid: False)

    ws_mod._handle_stop({"id": "steve"}, "main-1", "")
    await _settle_background_work()

    assert not current.is_set()


@pytest.mark.asyncio
async def test_wire_stop_with_explicit_null_turn_id_does_not_resolve_current_turn(
    main_db, monkeypatch,
):
    """Only an omitted wire field may trigger server-side Stop resolution."""
    from fastapi import WebSocketDisconnect

    import ws as ws_mod
    from host_access import processes
    from subagents import cc_runtime, codex_runtime

    _identity, current = _active_turn(main_db, "turn-current")
    _use_fixture_db_for_stop_resolution(monkeypatch, main_db)
    monkeypatch.setattr(
        ws_mod, "_authenticate", AsyncMock(return_value={"id": "steve"}),
    )
    monkeypatch.setattr(ws_mod, "_cleanup_ws", lambda *_args: None)
    monkeypatch.setattr(ws_mod, "_owns_thread", AsyncMock(return_value=True))
    monkeypatch.setattr(cc_runtime, "has_pending_or_live", lambda _sid: False)
    monkeypatch.setattr(codex_runtime, "has_pending_or_live", lambda _sid: False)
    monkeypatch.setattr(
        processes.PROCESS_OWNER, "terminate_turn", AsyncMock(return_value=None),
    )

    ws = SimpleNamespace(
        accept=AsyncMock(),
        receive_json=AsyncMock(side_effect=[
            {"type": "stop", "thread_id": "main-1", "turn_id": None},
            WebSocketDisconnect(),
        ]),
        send_json=AsyncMock(),
    )
    await ws_mod.websocket_endpoint(ws)
    await _settle_background_work()

    assert not current.is_set()


@pytest.mark.asyncio
async def test_resolved_stop_cannot_cancel_successor_replacing_turn_before_snapshot(
    main_db, monkeypatch,
):
    import ws as ws_mod
    from core.turn_core import TurnIdentity
    from core.native_turn_control import TurnCancelEvent
    from infra import state
    from subagents import cc_runtime, codex_runtime

    source_identity, _source = _active_turn(main_db, "turn-source")
    successor = TurnCancelEvent()
    successor.bind_turn_identity(TurnIdentity("turn-successor", 2))

    async def _resolve_then_replace(_user, _sid):
        state._active_turns["main-1"] = successor
        return source_identity.turn_id

    resolver = AsyncMock(side_effect=_resolve_then_replace)
    monkeypatch.setattr(ws_mod, "_resolve_active_turn_id", resolver)
    monkeypatch.setattr(ws_mod, "_owns_thread", AsyncMock(return_value=True))
    monkeypatch.setattr(cc_runtime, "has_pending_or_live", lambda _sid: False)
    monkeypatch.setattr(codex_runtime, "has_pending_or_live", lambda _sid: False)

    ws_mod._handle_stop({"id": "steve"}, "main-1")
    await _settle_background_work()

    resolver.assert_awaited_once_with({"id": "steve"}, "main-1")
    assert not successor.is_set()


@pytest.mark.asyncio
async def test_known_explicit_stop_still_cancels_its_current_turn(main_db, monkeypatch):
    import ws as ws_mod
    from host_access import processes
    from subagents import cc_runtime, codex_runtime

    identity, event = _active_turn(main_db, "turn-current")
    _use_fixture_db_for_stop_resolution(monkeypatch, main_db)
    monkeypatch.setattr(ws_mod, "_owns_thread", AsyncMock(return_value=True))
    monkeypatch.setattr(cc_runtime, "has_pending_or_live", lambda _sid: False)
    monkeypatch.setattr(codex_runtime, "has_pending_or_live", lambda _sid: False)
    monkeypatch.setattr(
        processes.PROCESS_OWNER, "terminate_turn", AsyncMock(return_value=None),
    )

    ws_mod._handle_stop({"id": "steve"}, "main-1", identity.turn_id)
    await _settle_background_work()

    assert event.is_set()
