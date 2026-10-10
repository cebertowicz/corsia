"""Codex checkpoint replacement keeps one Turn and one usable continuation."""

from __future__ import annotations

from llm.context_window import WindowFacts
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _admitted_input():
    from infra.database import get_db
    from infra.thread_driver import admit_input

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','codex/luna','t0','t0')"
    )
    db.commit()
    admission = admit_input(
        "thread-1",
        role="user",
        content="continue after checkpoint",
        intent="apply_now",
        message_id="input-1",
        turn_id="turn-1",
        boot_epoch="boot-1",
        admitted_at="t1",
        db=db,
    )
    db.close()
    assert admission.batch is not None
    return admission.batch


def _target():
    return SimpleNamespace(
        harness="codex",
        available=True,
        catalog_id="codex/luna",
        transport_model="gpt-5.6-luna",
        context_window=258_400,
        windows=WindowFacts(258_400, None),
        expected_reported_model="gpt-5.6-luna",
    )


def _live(batch, target):
    from subagents.codex_runtime_state import _LiveCodex

    return _LiveCodex(
        codex_thread_id="provider-old",
        session=SimpleNamespace(
            binary="codex", working_dir="/tmp", project_id=None, effort="high",
        ),
        parent_thread_id=None,
        name="codex",
        catalog_model_id=target.catalog_id,
        corsia_turn=batch.identity,
    )


def _prepared_batch(batch, target, prepared):
    return SimpleNamespace(
        batch=batch,
        thread_row={"id": batch.thread_id, "user_id": "user-1"},
        target=target,
        provider="codex",
        is_subagent=False,
        content="continue after checkpoint",
        agent_envelope=None,
        attachments=[],
        media_projection=None,
        prepared_context=prepared,
        refresh_prepared_context=None,
        runtime_truth={"codex_has_thread": True},
    )


def _checkpoint_tail_context(thread_id: str, target, current_user_text: str):
    from core.context_payload import (
        PreparedContextPayload,
        RenderedDestinationPayload,
    )
    from subagents.codex_destination import build_prepared_codex_destination

    snapshot = SimpleNamespace(
        to_dict=lambda: {"snapshot_id": "checkpoint-replacement"},
        source_inventory=(),
    )
    reconstruction = SimpleNamespace(
        task_text="checkpoint and tail",
        turn_text=current_user_text,
        snapshot=snapshot,
        history_items=(),
        checkpoint_text=None,
    )
    destination = build_prepared_codex_destination(
        thread_id=thread_id,
        target_catalog_id=target.catalog_id,
        current_user_text=current_user_text,
        reconstruction=reconstruction,
        delivery_mode="checkpoint_tail",
        handle_fingerprint=None,
        tool_registry=object(),
        dynamic_tools=(),
    )
    return PreparedContextPayload(
        mode="checkpoint_tail",
        target_catalog_id=target.catalog_id,
        model_context_window=target.context_window,
        history_epoch=1,
        sealed_boundary=None,
        replay=SimpleNamespace(mode="checkpoint_tail"),
        rendered=RenderedDestinationPayload(
            payload=destination,
            input_tokens=destination.input_tokens,
        ),
        runtime_context_epoch=1,
    )


@pytest.mark.asyncio
async def test_outer_write_owner_settles_codex_rejection_before_turn_terminal(
    isolated_db,
) -> None:
    """Runtime rejection must not terminalize ahead of its durable write owner."""
    from infra.database import get_db
    from infra.input_writes import begin_batch_write
    from subagents import codex_runtime as runtime
    from subagents.codex_app_server_errors import CodexAppServerError
    from subagents.codex_runtime_state import _LiveCodex

    batch = _admitted_input()
    db = get_db()
    begin_batch_write(batch, attempt_id="attempt-1", started_at="t2", db=db)
    db.close()
    live = _LiveCodex(
        codex_thread_id="provider-old",
        session=SimpleNamespace(_closing=False),
        parent_thread_id=None,
        name="codex",
        corsia_turn=batch.identity,
    )

    async def reject(_on_write, _on_drain):
        raise CodexAppServerError("provider rejected", {"code": -32600})

    async def settle_later():
        return None

    with pytest.raises(CodexAppServerError, match="provider rejected"):
        await runtime._accept_app_server_input(
            batch.thread_id,
            live,
            reject,
            mark_accepted=None,
            settle_delivery=settle_later,
            bcast=lambda *_args: None,
        )

    db = get_db()
    try:
        turn = db.execute(
            "SELECT status FROM active_turns WHERE id='turn-1'"
        ).fetchone()
        message = db.execute(
            "SELECT delivery_state,write_outcome FROM messages WHERE id='input-1'"
        ).fetchone()
    finally:
        db.close()
    assert turn["status"] == "running"
    assert tuple(message) == ("queued", None)


@pytest.mark.asyncio
async def test_rejected_checkpoint_continuation_preserves_durable_warm_source(
    isolated_db, monkeypatch,
) -> None:
    """A failed replacement cannot mask its error or discard the prior rollout."""
    from infra.database import get_db
    import infra.send_dispatch as send_dispatch
    import infra.thread_execution as execution
    from registry.service import register_codex_thread
    from subagents import codex_runtime as runtime
    from subagents import codex_turn_transitions as transitions

    batch = _admitted_input()
    db = get_db()
    register_codex_thread(
        batch.thread_id,
        "provider-old",
        rollout_path="/tmp/provider-old.jsonl",
        db=db,
    )
    db.close()
    target = _target()
    prepared = SimpleNamespace(runtime_context_epoch=1)
    live = _live(batch, target)
    runtime._LIVE.put(batch.thread_id, live)
    runtime._CODEX_THREADS[batch.thread_id] = "provider-old"
    retired: list[str | None] = []

    async def prepare(*_args, **_kwargs):
        return _prepared_batch(batch, target, prepared)

    async def reject_replacement(*_args, **_kwargs):
        raise ValueError("replacement rejected")

    async def accept(_provider, context):
        context.transfer_delivery_to_turn()
        await transitions.resume_same_turn_after_checkpoint_locked(
            batch.thread_id,
            live,
            prepared,
            current_user_text=context.content,
            mark_accepted=context.mark_accepted,
            mark_ambiguous=context.mark_ambiguous,
            settle_delivery=context.settle_delivery,
            settle_delivery_terminal=context.settle_delivery_terminal,
            held_lock=runtime._LIVE.lock(batch.thread_id),
        )

    async def fenced(*_args, **_kwargs):
        return True

    monkeypatch.setattr(execution, "_prepare_batch", prepare)
    monkeypatch.setattr(send_dispatch, "dispatch", accept)
    monkeypatch.setattr(runtime, "_spawn_turn", reject_replacement)
    monkeypatch.setattr(runtime, "_fence_exact_live", fenced)
    monkeypatch.setattr(
        runtime,
        "_retire_registered_thread_artifacts",
        lambda _sid, **kwargs: retired.append(kwargs.get("keep_thread_id")),
    )
    monkeypatch.setattr(
        "llm.resolution.resolve_execution_target", lambda _model: target,
    )

    try:
        with pytest.raises(transitions.CommittedTerminalFailure) as failure:
            await execution.execute_claimed_batch(
                batch,
                thread_row={"id": batch.thread_id, "user_id": "user-1"},
                target=target,
            )
        assert isinstance(failure.value.publication_error, ValueError)
        assert str(failure.value.publication_error) == "replacement rejected"
        assert failure.value.settlement is not None
        await transitions.drive_turn_settlement(failure.value.settlement)
        assert batch.thread_id not in runtime._CODEX_THREADS
        assert retired == []
        restored = await runtime._restore_thread_id(batch.thread_id, None)
        assert restored == "provider-old"
        assert runtime._CODEX_THREADS[batch.thread_id] == "provider-old"
    finally:
        runtime._LIVE.pop(batch.thread_id, None)
        runtime._CODEX_THREADS.pop(batch.thread_id, None)

    db = get_db()
    try:
        turn = db.execute(
            "SELECT status,error FROM active_turns WHERE id='turn-1'"
        ).fetchone()
        message = db.execute(
            "SELECT delivery_state,write_outcome FROM messages WHERE id='input-1'"
        ).fetchone()
    finally:
        db.close()
    assert tuple(turn) == ("error", "replacement rejected")
    assert tuple(message) == ("failed", "proved_no_write")


@pytest.mark.asyncio
async def test_accepted_checkpoint_continuation_retires_cache_then_warm_source(
    monkeypatch,
) -> None:
    """The old rollout remains available until its replacement is accepted."""
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime as runtime
    from subagents import codex_turn_transitions as transitions
    from subagents.codex_runtime_state import _LiveCodex

    identity = TurnIdentity("turn-1", 1)
    target = SimpleNamespace(
        harness="codex",
        available=True,
        catalog_id="codex/luna",
        transport_model="gpt-5.6-luna",
        context_window=258_400,
        windows=WindowFacts(258_400, None),
        expected_reported_model="gpt-5.6-luna",
    )
    session = SimpleNamespace(
        binary="codex",
        working_dir="/tmp",
        project_id=None,
        effort="high",
        terminate=AsyncMock(),
    )
    live = _LiveCodex(
        codex_thread_id="provider-old",
        session=session,
        parent_thread_id=None,
        name="codex",
        catalog_model_id=target.catalog_id,
        corsia_turn=identity,
    )
    runtime._LIVE.put("thread-1", live)
    runtime._APP_SESSIONS["thread-1"] = session
    runtime._CODEX_THREADS["thread-1"] = "provider-old"
    prepared = _checkpoint_tail_context("thread-1", target, "continue")
    kept: list[str | None] = []
    live.delivery_event_gate = asyncio.Event()

    async def deferred_event():
        await live.delivery_event_gate.wait()
        assert live.corsia_turn == identity

    live.delivery_event_tail = asyncio.create_task(deferred_event())

    async def start_provider(*_args, **kwargs):
        assert live.delivery_event_tail.done()
        assert kwargs["codex_thread_id"] is None
        runtime._CODEX_THREADS["thread-1"] = "provider-new"
        return identity

    monkeypatch.setattr(runtime, "_spawn_app_server_turn", start_provider)
    # The replacement spawn checks the host's Codex connection first; this test
    # is about rollout retirement, not whether this machine is signed in.
    monkeypatch.setattr(runtime, "_spawn_binary", lambda binary, _db: binary)
    monkeypatch.setattr(
        runtime,
        "_retire_registered_thread_artifacts",
        lambda _sid, **kwargs: kept.append(kwargs.get("keep_thread_id")),
    )
    monkeypatch.setattr(
        "llm.resolution.resolve_execution_target", lambda _model: target,
    )
    try:
        result = await transitions.resume_same_turn_after_checkpoint_locked(
            "thread-1",
            live,
            prepared,
            current_user_text="continue",
            held_lock=runtime._LIVE.lock("thread-1"),
        )
        assert result == identity
        assert kept == ["provider-new"]
        session.terminate.assert_awaited_once()
    finally:
        runtime._LIVE.pop("thread-1", None)
        runtime._APP_SESSIONS.pop("thread-1", None)
        runtime._CODEX_THREADS.pop("thread-1", None)


@pytest.mark.asyncio
async def test_checkpoint_continuation_retires_artifacts_off_the_event_loop(
    isolated_db, monkeypatch,
) -> None:
    """The checkpoint replacement retires registered threads on the DB pool.

    With ``CORSIA_DB_LOOP_WRITE_STRICT=1`` an on-loop ``write_transaction`` in
    ``_retire_registered_thread_artifacts`` raises ``LoopThreadWriteError``; the
    real retirement (not a stub) must run off the event loop.
    """
    from infra.db_loop_guard import LOOP_WRITE_STRICT_ENV
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime as runtime
    from subagents import codex_turn_transitions as transitions
    from subagents.codex_runtime_state import _LiveCodex

    monkeypatch.setenv(LOOP_WRITE_STRICT_ENV, "1")
    identity = TurnIdentity("turn-1", 1)
    target = SimpleNamespace(
        harness="codex",
        available=True,
        catalog_id="codex/luna",
        transport_model="gpt-5.6-luna",
        context_window=258_400,
        windows=WindowFacts(258_400, None),
        expected_reported_model="gpt-5.6-luna",
    )
    session = SimpleNamespace(
        binary="codex", working_dir="/tmp", project_id=None, effort="high",
        terminate=AsyncMock(),
    )
    live = _LiveCodex(
        codex_thread_id="provider-old",
        session=session,
        parent_thread_id=None,
        name="codex",
        catalog_model_id=target.catalog_id,
        corsia_turn=identity,
    )
    runtime._LIVE.put("thread-1", live)
    runtime._APP_SESSIONS["thread-1"] = session
    runtime._CODEX_THREADS["thread-1"] = "provider-old"
    prepared = _checkpoint_tail_context("thread-1", target, "continue")
    live.delivery_event_gate = asyncio.Event()

    async def deferred_event():
        await live.delivery_event_gate.wait()

    live.delivery_event_tail = asyncio.create_task(deferred_event())

    async def start_provider(*_args, **kwargs):
        runtime._CODEX_THREADS["thread-1"] = "provider-new"
        return identity

    monkeypatch.setattr(runtime, "_spawn_app_server_turn", start_provider)
    monkeypatch.setattr(runtime, "_spawn_binary", lambda binary, _db: binary)
    monkeypatch.setattr(
        "llm.resolution.resolve_execution_target", lambda _model: target,
    )
    try:
        result = await transitions.resume_same_turn_after_checkpoint_locked(
            "thread-1",
            live,
            prepared,
            current_user_text="continue",
            held_lock=runtime._LIVE.lock("thread-1"),
        )
        assert result == identity
        assert runtime._CODEX_THREADS["thread-1"] == "provider-new"
    finally:
        live.delivery_event_gate.set()
        runtime._LIVE.pop("thread-1", None)
        runtime._APP_SESSIONS.pop("thread-1", None)
        runtime._CODEX_THREADS.pop("thread-1", None)
