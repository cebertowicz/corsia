from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest
from fastapi import HTTPException


def test_runtime_quiescence_uses_thread_canonical_names() -> None:
    from thread import model_state, runtime_boundary

    assert model_state.ThreadQuiescenceError is runtime_boundary.ThreadQuiescenceError
    assert callable(runtime_boundary.quiesce_thread_runtimes)
    assert not hasattr(model_state, "SessionQuiescenceError")
    assert not hasattr(runtime_boundary, "quiesce_session_runtimes")


def _seed_boundary(thread_id: str) -> tuple[str, str]:
    from infra.database import get_db
    from infra.queries import insert_message

    anchor_id = f"{thread_id}-anchor"
    after_id = f"{thread_id}-after"
    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads "
            "(id, user_id, model, created_at, updated_at) "
            "VALUES (?, 'steve', 'test/model', 't', 't')",
            (thread_id,),
        )
        insert_message(
            thread_id,
            "user",
            "replace me",
            {"turn_unbound_reason": "legacy_import"},
            msg_id=anchor_id,
            db=db,
            created_at="2026-07-14T01:00:00Z",
        )
        insert_message(
            thread_id,
            "assistant",
            "old answer",
            {"turn_unbound_reason": "legacy_import"},
            msg_id=after_id,
            db=db,
            created_at="2026-07-14T01:00:01Z",
        )
        db.commit()
    finally:
        db.close()
    return anchor_id, after_id


def _row_count(table: str, column: str, value: str) -> int:
    from infra.database import get_db

    db = get_db()
    try:
        return int(db.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {column} = ?",
            (value,),
        ).fetchone()["n"])
    finally:
        db.close()


def _history_epoch(thread_id: str) -> int:
    from infra.database import get_db

    db = get_db()
    try:
        return int(db.execute(
            "SELECT history_epoch FROM threads WHERE id = ?", (thread_id,),
        ).fetchone()["history_epoch"])
    finally:
        db.close()


def _admit_turn(thread_id: str, turn_id: str):
    from infra.thread_driver import admit_input

    admission = admit_input(
        thread_id,
        role="user",
        content="active input",
        intent="apply_now",
        message_id=f"{turn_id}-input",
        turn_id=turn_id,
        boot_epoch="test-boot",
        admitted_at="2026-07-14T01:00:01.500000Z",
    )
    assert admission.batch is not None
    return admission.batch.identity


async def _call_boundary(thread_id: str):
    from api.thread_mutations import delete_thread

    return await delete_thread(thread_id, "tok")


def _patch_route_shell(monkeypatch) -> None:
    from api import thread_mutations as sessions

    authenticate = lambda token: {"id": "steve"} if token == "tok" else None
    monkeypatch.setattr(sessions, "authenticate", authenticate)
    monkeypatch.setattr(sessions, "emit_product_event", lambda *_a, **_kw: None)


def _append_late_and_terminal(
    thread_id: str,
    turn_id: str,
    outcome: str,
):
    from core.turn_core import persist_turn_terminal
    from infra.database import get_db
    from infra.queries import insert_message

    db = get_db()
    try:
        assert db.execute(
            "SELECT 1 FROM threads WHERE id = ?", (thread_id,),
        ).fetchone() is not None
        assert db.execute(
            "SELECT status FROM active_turns WHERE id = ?", (turn_id,),
        ).fetchone()["status"] == "running"
        insert_message(
            thread_id,
            "assistant",
            "late provider output",
            {"turn_id": turn_id},
            msg_id=f"{thread_id}-late",
            db=db,
            created_at="2026-07-14T01:00:02Z",
        )
        terminal = persist_turn_terminal(turn_id, outcome, db=db)
        db.commit()
        return terminal
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["native", "cc", "codex"])
async def test_destructive_boundary_joins_provider_before_mutation(
    isolated_db, monkeypatch, provider,
):
    from core.native_turn_control import TurnCancelEvent
    from core.turn_core import TurnTerminalResult
    from infra import state
    from subagents import cc_runtime, codex_runtime

    _patch_route_shell(monkeypatch)
    thread_id = f"delete-{provider}-quiesce"
    anchor_id, _ = _seed_boundary(thread_id)
    turn_id = f"{thread_id}-turn"
    identity = _admit_turn(thread_id, turn_id)
    cancel = None
    observed: list[str] = []
    terminals: list[TurnTerminalResult] = []
    pending = {"cc": provider == "cc", "codex": provider == "codex"}
    native_task: asyncio.Task | None = None
    llm_task: asyncio.Task | None = None

    async def _retire(kind: str) -> bool:
        if provider != kind:
            return False
        observed.append(kind)
        terminals.append(_append_late_and_terminal(
            thread_id, turn_id, "cancelled",
        ))
        pending[kind] = False
        return True

    monkeypatch.setattr(
        cc_runtime, "has_pending_or_live", lambda sid: pending["cc"],
    )
    monkeypatch.setattr(cc_runtime, "is_live", lambda sid: provider == "cc")
    monkeypatch.setattr(cc_runtime, "retire", lambda sid, **_kw: _retire("cc"))
    monkeypatch.setattr(
        codex_runtime, "retire", lambda sid, **_kw: _retire("codex"),
    )
    monkeypatch.setattr(
        codex_runtime, "has_pending_or_live", lambda sid: pending["codex"],
    )

    if provider == "native":
        cancel = TurnCancelEvent()
        cancel.bind_turn_identity(identity)

        async def _native_runner() -> None:
            await cancel.wait()
            observed.append("native")
            terminals.append(_append_late_and_terminal(
                thread_id, turn_id, "cancelled",
            ))
            if state._active_turns.get(thread_id) is cancel:
                state._active_turns.pop(thread_id, None)
            if state._active_turn_tasks.get(thread_id) is native_task:
                state._active_turn_tasks.pop(thread_id, None)
            if state._active_llm_tasks.get(thread_id) is llm_task:
                state._active_llm_tasks.pop(thread_id, None)
            cancel.completion.set_result(True)

        native_task = asyncio.create_task(_native_runner())
        llm_task = asyncio.create_task(asyncio.sleep(3600))
        state._active_turns[thread_id] = cancel
        state._active_turn_tasks[thread_id] = native_task
        state._active_llm_tasks[thread_id] = llm_task

    try:
        result = await _call_boundary(thread_id)
        assert result["ok"] is True
        assert observed == [provider]
        assert terminals == [TurnTerminalResult.APPLIED]
        assert _row_count("active_turns", "thread_id", thread_id) == 0
        assert _row_count("messages", "id", f"{thread_id}-late") == 0
        assert _row_count("threads", "id", thread_id) == 0
    finally:
        state._active_turns.pop(thread_id, None)
        state._active_turn_tasks.pop(thread_id, None)
        state._active_llm_tasks.pop(thread_id, None)
        for task in (native_task, llm_task):
            if task is not None and not task.done():
                task.cancel()
            if task is not None:
                with suppress(asyncio.CancelledError):
                    await task


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["native", "cc", "codex"])
async def test_destructive_boundary_fails_closed_when_quiescence_fails(
    isolated_db, monkeypatch, provider,
):
    from core.native_turn_control import TurnCancelEvent
    from infra import state
    from thread import runtime_boundary
    from subagents import cc_runtime, codex_runtime

    _patch_route_shell(monkeypatch)
    monkeypatch.setattr(
        runtime_boundary, "_DESTRUCTIVE_QUIESCE_TIMEOUT_S", 0.01,
        raising=False,
    )
    thread_id = f"delete-{provider}-quiesce-failure"
    anchor_id, after_id = _seed_boundary(thread_id)
    turn_id = f"{thread_id}-turn"
    identity = _admit_turn(thread_id, turn_id)
    cancel = None

    async def _retire(kind: str) -> bool:
        if provider == kind:
            raise RuntimeError(f"{kind} teardown failed")
        return False

    monkeypatch.setattr(
        cc_runtime, "has_pending_or_live", lambda sid: provider == "cc",
    )
    monkeypatch.setattr(cc_runtime, "is_live", lambda sid: provider == "cc")
    monkeypatch.setattr(cc_runtime, "retire", lambda sid, **_kw: _retire("cc"))
    monkeypatch.setattr(
        codex_runtime, "retire", lambda sid, **_kw: _retire("codex"),
    )
    monkeypatch.setattr(
        codex_runtime,
        "has_pending_or_live",
        lambda sid: provider == "codex",
    )

    if provider == "native":
        cancel = TurnCancelEvent()
        cancel.bind_turn_identity(identity)
        state._active_turns[thread_id] = cancel

    try:
        with pytest.raises(HTTPException) as exc_info:
            await _call_boundary(thread_id)
        assert exc_info.value.status_code == 409
        assert _row_count("threads", "id", thread_id) == 1
        assert _row_count("active_turns", "thread_id", thread_id) == 1
        assert _row_count("messages", "id", anchor_id) == 1
        assert _row_count("messages", "id", after_id) == 1
        assert _history_epoch(thread_id) == 0
        if cancel is not None:
            assert cancel.replacement_requested is False
    finally:
        state._active_turns.pop(thread_id, None)


@pytest.mark.asyncio
async def test_the_delete_fence_refuses_a_successor_turn(
    isolated_db, monkeypatch,
):
    from api import thread_mutations as sessions
    from core.turn_core import persist_turn_terminal
    from infra.send_types import SendRejected

    _patch_route_shell(monkeypatch)
    thread_id = "delete-successor-race"
    _seed_boundary(thread_id)
    turn_id = f"{thread_id}-turn"
    _admit_turn(thread_id, turn_id)
    refusals: list[int] = []

    async def _quiesce(_thread_id: str, *, user_id: str | None):
        assert _thread_id == thread_id
        assert user_id == "steve"
        assert persist_turn_terminal(turn_id, "cancelled").value == "applied"
        with pytest.raises(SendRejected) as refused:
            _admit_turn(thread_id, f"{thread_id}-successor")
        refusals.append(refused.value.status_code)
        return {
            "native_cancelled": False,
            "cc_stopped": False,
            "codex_retired": False,
        }

    monkeypatch.setattr(
        sessions.runtime_boundary, "quiesce_thread_runtimes", _quiesce,
    )

    await _call_boundary(thread_id)
    assert refusals == [409]
    assert _row_count("threads", "id", thread_id) == 0
    assert _row_count("active_turns", "id", f"{thread_id}-successor") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["cc", "codex"])
async def test_destructive_boundary_rejects_unretired_idle_provider(
    isolated_db, monkeypatch, provider,
):
    from subagents import cc_runtime, codex_runtime

    _patch_route_shell(monkeypatch)
    thread_id = f"delete-{provider}-idle-retire-failure"
    anchor_id, after_id = _seed_boundary(thread_id)
    retired: list[str] = []

    async def _retire(kind: str) -> bool:
        if provider == kind:
            retired.append(kind)
        return False

    monkeypatch.setattr(
        cc_runtime, "has_pending_or_live", lambda sid: provider == "cc",
    )
    monkeypatch.setattr(cc_runtime, "retire", lambda sid, **_kw: _retire("cc"))
    monkeypatch.setattr(
        codex_runtime,
        "has_pending_or_live",
        lambda sid: provider == "codex",
        raising=False,
    )
    monkeypatch.setattr(
        codex_runtime, "retire", lambda sid, **_kw: _retire("codex"),
    )

    with pytest.raises(HTTPException) as exc_info:
        await _call_boundary(thread_id)
    assert exc_info.value.status_code == 409
    assert "did not stop cleanly" in str(exc_info.value.detail)
    assert retired == [provider]
    assert _row_count("threads", "id", thread_id) == 1
    assert _row_count("messages", "id", anchor_id) == 1
    assert _row_count("messages", "id", after_id) == 1


@pytest.mark.asyncio
async def test_the_delete_fence_refuses_a_successor_input(
    isolated_db, monkeypatch,
):
    from api import thread_mutations as sessions
    from infra.database import get_db
    from infra.input_admission import accept_thread_input
    from infra.send_types import SendRejected

    _patch_route_shell(monkeypatch)
    thread_id = "delete-successor-input-race"
    _seed_boundary(thread_id)
    successor_id = f"{thread_id}-successor-user"
    refusals: list[int] = []

    async def _quiesce(_thread_id: str, *, user_id: str | None):
        assert _thread_id == thread_id
        assert user_id == "steve"
        db = get_db()
        try:
            with pytest.raises(SendRejected) as refused:
                accept_thread_input(
                    thread_id,
                    role="user",
                    content="racing successor",
                    intent="apply_now",
                    message_id=successor_id,
                    db=db,
                    created_at="2026-07-14T01:00:03Z",
                )
            refusals.append(refused.value.status_code)
        finally:
            db.close()
        return {
            "native_cancelled": False,
            "cc_stopped": False,
            "codex_retired": False,
        }

    monkeypatch.setattr(
        sessions.runtime_boundary, "quiesce_thread_runtimes", _quiesce,
    )

    await _call_boundary(thread_id)
    assert refusals == [409]
    assert _row_count("threads", "id", thread_id) == 0
    assert _row_count("messages", "id", successor_id) == 0
