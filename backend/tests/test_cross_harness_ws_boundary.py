from __future__ import annotations

import asyncio
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared_send_fixtures import _insert_session


_MODEL = {
    "native": "oMLX/qwen",
    "cc": "claude-code/sonnet",
    "codex": "codex/gpt-5-codex",
}


def _admit_identity(
    thread_id: str,
    turn_id: str,
    model: str,
    text: str,
    *,
    db=None,
    settle: bool = False,
):
    from core.turn_core import BOOT_EPOCH
    from infra.thread_driver import admit_input
    from infra.time import utc_now_iso

    admission = admit_input(
        thread_id,
        role="user",
        content=text,
        intent="apply_now",
        message_id=f"{turn_id}-input",
        turn_id=turn_id,
        boot_epoch=BOOT_EPOCH,
        admitted_at=utc_now_iso(),
        db=db,
    )
    assert admission.batch is not None
    if settle:
        from infra.input_writes import begin_batch_write, settle_batch_write

        attempt_id = f"test-write:{turn_id}"
        begin_batch_write(
            admission.batch,
            attempt_id=attempt_id,
            started_at=utc_now_iso(),
            db=db,
        )
        settle_batch_write(attempt_id, outcome="accepted", db=db)
    return admission.batch.identity


def _append_settlement_and_terminal(
    thread_id: str,
    turn_id: str,
    source: str,
    *,
    outcome: str = "cancelled",
) -> None:
    from core.turn_core import TurnTerminalResult, persist_turn_terminal
    from infra.database import get_db
    from infra.queries import insert_message

    db = get_db()
    try:
        insert_message(
            thread_id,
            "assistant",
            f"{source} predecessor settled",
            {},
            msg_id=f"{thread_id}-{source}-settled",
            created_at="2026-07-14T20:00:01Z",
            db=db,
        )
        assert persist_turn_terminal(
            turn_id, outcome, db=db,
        ) is TurnTerminalResult.APPLIED
        db.commit()
    finally:
        db.close()


def _turn_status(turn_id: str) -> str:
    from infra.database import get_db

    db = get_db()
    try:
        return str(db.execute(
            "SELECT status FROM active_turns WHERE id = ?",
            (turn_id,),
        ).fetchone()["status"])
    finally:
        db.close()


def _install_predecessor(
    monkeypatch,
    *,
    thread_id: str,
    source: str,
    order: list[str],
) -> tuple[str, list[asyncio.Task], object | None]:
    from core.native_turn_control import TurnCancelEvent
    from infra import state
    from subagents import cc_runtime, codex_runtime

    turn_id = f"{thread_id}-{source}-turn"
    identity = _admit_identity(
        thread_id,
        turn_id,
        _MODEL[source],
        "predecessor input",
        db=None,
        settle=True,
    )
    pending = {"cc": source == "cc", "codex": source == "codex"}

    async def _retire(kind: str) -> bool:
        if not pending[kind]:
            return False
        order.append("settle")
        _append_settlement_and_terminal(thread_id, turn_id, source)
        pending[kind] = False
        return True

    async def _stop(kind: str, expected_turn_id: str | None) -> bool:
        if not pending[kind]:
            return False
        assert expected_turn_id == turn_id
        return await _retire(kind)

    monkeypatch.setattr(
        cc_runtime, "has_pending_or_live", lambda _sid: pending["cc"],
    )
    monkeypatch.setattr(
        cc_runtime, "is_live", lambda _sid: pending["cc"],
    )
    monkeypatch.setattr(
        cc_runtime, "retire", lambda _sid, **_kw: _retire("cc"),
    )
    monkeypatch.setattr(
        cc_runtime,
        "stop",
        lambda _sid, expected_turn_id=None, **_kw: _stop(
            "cc", expected_turn_id,
        ),
    )
    monkeypatch.setattr(
        codex_runtime, "has_pending_or_live", lambda _sid: pending["codex"],
    )
    monkeypatch.setattr(
        codex_runtime, "is_live", lambda _sid: pending["codex"],
    )
    monkeypatch.setattr(
        codex_runtime, "retire", lambda _sid, **_kw: _retire("codex"),
    )
    monkeypatch.setattr(
        codex_runtime,
        "stop",
        lambda _sid, expected_turn_id=None, **_kw: _stop(
            "codex", expected_turn_id,
        ),
    )

    tasks: list[asyncio.Task] = []
    cancel_event = None
    if source == "native":
        cancel_event = TurnCancelEvent()
        cancel_event.bind_turn_identity(identity)
        native_task: asyncio.Task | None = None
        llm_task = asyncio.create_task(asyncio.sleep(3600))

        async def _native_runner() -> None:
            await cancel_event.wait()
            order.append("settle")
            _append_settlement_and_terminal(
                thread_id,
                turn_id,
                source,
                outcome="cancelled",
            )
            if state._active_turns.get(thread_id) is cancel_event:
                state._active_turns.pop(thread_id, None)
            if state._active_turn_tasks.get(thread_id) is native_task:
                state._active_turn_tasks.pop(thread_id, None)
            if state._active_llm_tasks.get(thread_id) is llm_task:
                state._active_llm_tasks.pop(thread_id, None)
            if not cancel_event.completion.done():
                cancel_event.completion.set_result(True)

        native_task = asyncio.create_task(_native_runner())
        tasks.extend([native_task, llm_task])
        state._active_turns[thread_id] = cancel_event
        state._active_turn_tasks[thread_id] = native_task
        state._active_llm_tasks[thread_id] = llm_task

    return turn_id, tasks, cancel_event


async def _cleanup_native(
    thread_id: str,
    tasks: list[asyncio.Task],
    cancel_event,
) -> None:
    from infra import state

    state._active_turns.pop(thread_id, None)
    state._active_turn_tasks.pop(thread_id, None)
    state._active_llm_tasks.pop(thread_id, None)
    if cancel_event is not None and not cancel_event.completion.done():
        cancel_event.completion.cancel()
    for task in tasks:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_exact_quiesce_does_not_cancel_newer_native_owner(
    isolated_db,
):
    from core.native_turn_control import TurnCancelEvent
    from core.turn_core import persist_turn_terminal
    from infra import state
    from thread.model_state import ThreadQuiescenceError
    from thread.runtime_boundary import (
        quiesce_exact_turn_owner,
    )

    thread_id = "native-owner-race"
    _insert_session(
        thread_id,
        user_id="user-1",
        model=_MODEL["native"],
        spawned_by=None,
    )
    predecessor = _admit_identity(
        thread_id,
        "native-owner-old",
        _MODEL["native"],
        "old",
        settle=True,
    )
    persist_turn_terminal(predecessor.turn_id, "completed")
    successor = _admit_identity(
        thread_id,
        "native-owner-new",
        _MODEL["native"],
        "new",
    )
    cancel_event = TurnCancelEvent()
    cancel_event.bind_turn_identity(successor)
    state._active_turns[thread_id] = cancel_event
    try:
        with pytest.raises(ThreadQuiescenceError, match="owner changed"):
            await quiesce_exact_turn_owner(
                thread_id,
                expected_turn=predecessor,
                user_id="user-1",
        )
        assert cancel_event.is_set() is False
        assert cancel_event.checkpoint_wait_scope.stop_requested is False
        assert _turn_status(successor.turn_id) == "running"
    finally:
        state._active_turns.pop(thread_id, None)
        cancel_event.completion.cancel()


@pytest.mark.asyncio
async def test_native_cancel_join_fences_expected_identity() -> None:
    from core.turn_core import TurnIdentity
    from core.native_turn_control import TurnCancelEvent, cancel_and_join_native_turn
    from infra import state

    thread_id = "native-cancel-inner-fence"
    expected = TurnIdentity("native-expected", 1)
    successor = TurnIdentity("native-successor", 2)
    cancel_event = TurnCancelEvent()
    cancel_event.bind_turn_identity(successor)
    state._active_turns[thread_id] = cancel_event
    try:
        with pytest.raises(RuntimeError, match="owner changed"):
            await cancel_and_join_native_turn(
                thread_id,
                expected_turn=expected,
        )
        assert cancel_event.is_set() is False
        assert cancel_event.checkpoint_wait_scope.stop_requested is False
    finally:
        state._active_turns.pop(thread_id, None)
        cancel_event.completion.cancel()


async def _async_false(*_args, **_kwargs) -> bool:
    return False


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["cc", "codex"])
async def test_provider_main_ws_wrapper_passes_socket_to_shared_send(
    monkeypatch,
    provider,
):
    from ws import message_dispatch

    ws = SimpleNamespace(send_json=_async_send_json)
    seen: dict = {}

    async def _send(*_args, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            driver=f"{provider}.accept",
            message_id="message",
            created_at="t",
            turn_id="turn",
            turn_order=1,
            duplicate=False,
        )

    session = {
        "id": f"wrapper-{provider}",
        "user_id": "user-1",
        "title": "Thread",
        "project_id": None,
    }
    if provider == "cc":
        await message_dispatch.handle_cc_main_message(
            ws,
            session,
            session["id"],
            "input",
            [],
            msg_id="",
            now="",
            is_retry=False,
            is_edited=False,
            input_id=None,
            send_thread_input_fn=_send,
            cc_attachment_block_fn=lambda *_args: "",
        )
    else:
        await message_dispatch.handle_codex_main_message(
            ws,
            session,
            session["id"],
            "input",
            msg_id="",
            now="",
            is_retry=False,
            input_id=None,
            send_thread_input_fn=_send,
        )

    assert seen["ws"] is ws


async def _async_send_json(_payload) -> None:
    return None
