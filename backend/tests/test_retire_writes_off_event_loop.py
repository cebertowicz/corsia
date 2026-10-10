"""Thread-retire and shutdown-marker writes never open on the event-loop thread.

With ``CORSIA_DB_LOOP_WRITE_STRICT=1`` an outermost ``write_transaction`` opened
on a thread running an asyncio loop raises ``LoopThreadWriteError``. Each scope
below is driven through its real async entry point, so a synchronous write would
fail loudly here; a write that moves to the DB pool writes the same rows.
"""

from __future__ import annotations

import pytest

from infra.db_loop_guard import LOOP_WRITE_STRICT_ENV


def _thread_row(thread_id: str, parent: str | None = None) -> None:
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "INSERT OR IGNORE INTO threads "
            "(id,user_id,model,created_at,updated_at,spawned_by) VALUES (?,?,?,?,?,?)",
            (thread_id, "steve", "codex/luna", "t0", "t0", parent),
        )
        db.commit()
    finally:
        db.close()


def _message_count(thread_id: str) -> int:
    from infra.database import get_db

    db = get_db()
    try:
        return db.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id=?", (thread_id,)
        ).fetchone()[0]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_codex_retire_artifacts_writes_off_the_event_loop(
    isolated_db, monkeypatch,
) -> None:
    """``_retire_locked`` retires registered threads on the DB pool, not the loop."""
    monkeypatch.setenv(LOOP_WRITE_STRICT_ENV, "1")
    _thread_row("thread-1")
    from subagents import codex_runtime

    assert await codex_runtime.retire("thread-1") is False


@pytest.mark.asyncio
async def test_codex_context_boundary_retire_writes_off_the_event_loop(
    isolated_db, monkeypatch,
) -> None:
    """The context-boundary entry point reaches the same off-loop retirement."""
    monkeypatch.setenv(LOOP_WRITE_STRICT_ENV, "1")
    _thread_row("thread-1")
    from thread import runtime_boundary

    effects = await runtime_boundary.retire_harness_continuations(
        "thread-1", user_id=None
    )
    assert effects["codex_retired"] is False


@pytest.mark.asyncio
async def test_codex_mark_dead_retires_off_the_event_loop(
    isolated_db, monkeypatch,
) -> None:
    """``_mark_dead_locked`` retires registered threads on the DB pool."""
    monkeypatch.setenv(LOOP_WRITE_STRICT_ENV, "1")
    _thread_row("thread-1")
    from subagents import codex_runtime

    await codex_runtime.mark_dead("thread-1")


@pytest.mark.asyncio
async def test_cc_retire_provider_session_writes_off_the_event_loop(
    isolated_db, monkeypatch,
) -> None:
    """``cc_shutdown.retire`` retires the durable Claude transcript off the loop."""
    monkeypatch.setenv(LOOP_WRITE_STRICT_ENV, "1")
    _thread_row("child-1", parent="thread-1")
    from subagents import cc_shutdown

    assert await cc_shutdown.retire("child-1") is False


@pytest.mark.asyncio
async def test_cc_retire_if_current_retires_off_the_event_loop(
    isolated_db, monkeypatch,
) -> None:
    """Both ``retire_if_current`` branches write the provider retirement off-loop."""
    monkeypatch.setenv(LOOP_WRITE_STRICT_ENV, "1")
    _thread_row("child-1", parent="thread-1")
    from subagents import cc_handle, cc_shutdown

    # expected_owner None, no live handle: retire the durable session inline-free.
    assert await cc_handle.retire_if_current("child-1", expected_owner=None) is True

    # expected_owner matches a live handle: fence it, then retire the session.
    monkeypatch.setattr(cc_handle, "live_handle_fingerprint", lambda *_a, **_k: "owner")

    async def _retired_live(*_a, **_k):
        return True

    monkeypatch.setattr(cc_shutdown, "retire_live", _retired_live)
    from subagents.cc_runtime_state import _Live

    live = _Live(session=None, parent_thread_id="thread-1", name="cc")
    cc_handle._LIVE.put("child-1", live)
    try:
        assert (
            await cc_handle.retire_if_current("child-1", expected_owner="owner")
            is True
        )
    finally:
        cc_handle._LIVE.pop("child-1", None)


@pytest.mark.asyncio
async def test_cc_shutdown_marker_writes_off_the_event_loop(
    isolated_db, monkeypatch,
) -> None:
    """A shutdown marker survives on-loop shutdown by writing on the DB pool."""
    monkeypatch.setenv(LOOP_WRITE_STRICT_ENV, "1")
    _thread_row("child-1", parent="thread-1")
    from core.turn_core import TurnIdentity
    from subagents import cc_shutdown
    from subagents.cc_runtime_state import _Live

    async def _prepared(*_a, **_k):
        return (1.0, [])

    async def _terminated(*_a, **_k):
        return (True, None)

    async def _joined(*_a, **_k):
        return True

    async def _finalized(_child_id, live, *_a, **_k):
        live.turn_active = False
        return True

    monkeypatch.setattr(cc_shutdown, "prepare_live_close", _prepared)
    monkeypatch.setattr(cc_shutdown, "_terminate_for_shutdown", _terminated)
    monkeypatch.setattr(cc_shutdown, "join_admitted_tool_tasks", _joined)
    monkeypatch.setattr(
        cc_shutdown, "settle_and_finalize_retired_live_for_identity", _finalized
    )
    live = _Live(
        session=None,
        parent_thread_id="thread-1",
        name="cc",
        corsia_turn=TurnIdentity("turn-1", 1),
        turn_active=True,
    )
    await cc_shutdown.shutdown_one_live("child-1", live)

    assert _message_count("child-1") == 1


@pytest.mark.asyncio
async def test_codex_shutdown_marker_writes_off_the_event_loop(
    isolated_db, monkeypatch,
) -> None:
    """The Codex shutdown marker is written on the DB pool, not the loop."""
    monkeypatch.setenv(LOOP_WRITE_STRICT_ENV, "1")
    _thread_row("thread-1")
    from core.turn_core import TurnIdentity, TurnTerminalResult
    from subagents import codex_runtime
    from subagents.codex_runtime_state import _LiveCodex

    async def _quiesced(*_a, **_k):
        return True

    async def _interrupted(*_a, **_k):
        return TurnTerminalResult.APPLIED

    async def _terminated(*_a, **_k):
        return True

    monkeypatch.setattr(codex_runtime, "_quiesce_tool_turn", _quiesced)
    monkeypatch.setattr(codex_runtime, "_finish_interrupted_turn", _interrupted)
    monkeypatch.setattr(codex_runtime, "_terminate_exact_live", _terminated)

    live = _LiveCodex(
        codex_thread_id="provider-1",
        session=None,
        parent_thread_id=None,
        name="codex",
        catalog_model_id="codex/luna",
        corsia_turn=TurnIdentity("turn-1", 1),
    )
    codex_runtime._LIVE.put("thread-1", live)
    try:
        await codex_runtime._shutdown_live_owner(
            "thread-1", live, codex_runtime._default_broadcast
        )
    finally:
        codex_runtime._LIVE.pop("thread-1", None)

    assert _message_count("thread-1") == 1