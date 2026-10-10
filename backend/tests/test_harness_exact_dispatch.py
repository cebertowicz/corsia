from __future__ import annotations

import inspect
import uuid
from unittest.mock import AsyncMock

import pytest

from shared_send_fixtures import prepared_context_with_fit_ledger


def _target(harness: str, selector: str, expected: str | None = None):
    from llm.resolution import ExecutionTarget

    return ExecutionTarget(
        catalog_id=f"{harness}/{selector}", harness=harness,
        provider=harness,
        backend={"native": "api", "claude-code": "cc", "codex": "codex"}[harness],
        transport_model=selector, expected_reported_model=expected,
        context_window=1000, available=True, availability_reason=None,
        display_name=selector, supports_reasoning=True,
    )


def test_codex_public_feed_seams_require_one_execution_target():
    from subagents import codex_runtime

    for name in ("ensure_live_then_feed", "feed", "feed_main"):
        parameters = inspect.signature(getattr(codex_runtime, name)).parameters
        assert parameters["target"].default is inspect.Parameter.empty
        assert "model" not in parameters
        assert "expected_reported_model" not in parameters


def test_durable_turn_owner_is_authoritative_over_input_metadata(isolated_db):
    import json

    from infra.database import get_db
    from infra.thread_driver import admit_input

    thread_id = "server-turn-owner"
    turn_id = str(uuid.uuid4())
    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads "
            "(id,user_id,model,created_at,updated_at) VALUES (?,?,?,?,?)",
            (thread_id, "owner", "claude-code/sonnet", "t", "t"),
        )
        db.commit()
        admission = admit_input(
            thread_id,
            role="user",
            content="hello",
            intent="apply_now",
            message_id="server-turn-owner-input",
            turn_id=turn_id,
            boot_epoch="boot",
            admitted_at="t",
            metadata={"turn_id": "caller-spoof", "source": "user"},
            db=db,
        )
        row = db.execute(
            "SELECT active_turn_id,metadata FROM messages WHERE id=?",
            ("server-turn-owner-input",),
        ).fetchone()
    finally:
        db.close()

    assert admission.batch is not None
    assert admission.batch.turn_id == turn_id
    assert row["active_turn_id"] == turn_id
    assert json.loads(row["metadata"])["turn_id"] == "caller-spoof"


@pytest.mark.asyncio
async def test_cc_sub_dispatch_forwards_exact_selector_and_expected_identity(monkeypatch):
    from core.turn_core import TurnIdentity
    from infra import effort
    from infra.send_dispatch import DispatchContext, _accept_cc
    from subagents import cc_runtime

    monkeypatch.setattr(effort, "resolve_effort", lambda *_: None)
    monkeypatch.setattr(
        "connections.models.connection_available", lambda _provider: True,
    )
    feed = AsyncMock(return_value=TurnIdentity("turn-cc-exact", 1))
    monkeypatch.setattr(cc_runtime, "feed", feed)

    target = _target(
        "claude-code", "claude-fable-5[1m]", "claude-fable-5"
    )
    ctx = DispatchContext(
        thread_id="cc-child",
        thread_row={
            "model": "claude-code/claude-fable-5[1m]",
            "project_id": None,
            "user_id": "owner",
        },
        content="continue",
        is_subagent=True,
        target=target,
        turn_id="turn-cc-exact",
        prepared_context=prepared_context_with_fit_ledger(target),
    )
    await _accept_cc(ctx)

    assert feed.await_args.kwargs["target"] is ctx.target


@pytest.mark.asyncio
async def test_cc_dispatch_rechecks_connection_immediately_before_feed(monkeypatch):
    from infra import effort
    from infra.send_dispatch import DispatchContext, _accept_cc
    from infra.send_types import SendRejected
    from subagents import cc_runtime

    monkeypatch.setattr(effort, "resolve_effort", lambda *_: None)
    monkeypatch.setattr(
        "connections.models.connection_available", lambda _provider: False,
    )
    feed = AsyncMock()
    monkeypatch.setattr(cc_runtime, "feed", feed)
    ctx = DispatchContext(
        thread_id="cc-child",
        thread_row={
            "model": "claude-code/sonnet",
            "project_id": None,
            "user_id": "owner",
        },
        content="continue",
        is_subagent=True,
        target=_target("claude-code", "sonnet", "claude-sonnet-5"),
        turn_id="turn-cc-unavailable",
    )

    with pytest.raises(SendRejected, match="connection is not available") as exc:
        await _accept_cc(ctx)

    assert exc.value.status_code == 503
    feed.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_cc_sub_feed_forwards_owner_broadcast(monkeypatch):
    from core.turn_core import TurnIdentity
    from subagents import cc_feed, cc_runtime

    broadcast = object()
    accepted = TurnIdentity("turn-owner-route", 9)
    ensure = AsyncMock(return_value=accepted)
    monkeypatch.setattr(cc_feed, "ensure_live_then_feed", ensure)
    target = _target("claude-code", "sonnet", "claude-sonnet-5")

    result = await cc_runtime.feed(
        "cc-child", "continue",
        target=target,
        turn_id="turn-owner-route",
        broadcast=broadcast,
        prepared_context=prepared_context_with_fit_ledger(target),
    )

    assert result is accepted
    assert ensure.await_args.kwargs["target"].catalog_id == "claude-code/sonnet"
    assert ensure.await_args.kwargs["broadcast"] is broadcast


@pytest.mark.asyncio
async def test_cc_stale_prepared_context_is_retryable_send_rejection(monkeypatch):
    from infra.send_dispatch import dispatch_cc_sub
    from infra.send_types import DeliveryFailureSide, SendRejected
    from subagents import cc_runtime

    monkeypatch.setattr(
        cc_runtime,
        "feed",
        AsyncMock(side_effect=cc_runtime.CCPreparedDestinationStale("changed")),
    )

    with pytest.raises(SendRejected) as rejected:
        await dispatch_cc_sub(
            "cc-child",
            "next",
            None,
            target=_target("claude-code", "sonnet", "sonnet"),
            turn_id="turn-stale-context",
            prepared_context=prepared_context_with_fit_ledger(
                _target("claude-code", "sonnet", "sonnet"),
            ),
        )

    assert rejected.value.status_code == 409
    assert rejected.value.metadata["cause"] == "prepared_context_stale"
    assert rejected.value.side is DeliveryFailureSide.PROVIDER


@pytest.mark.asyncio
async def test_codex_dispatch_forwards_saved_selection_and_expected_identity(monkeypatch):
    from core.turn_core import TurnIdentity
    from infra import effort
    from infra.send_dispatch import DispatchContext, _accept_codex
    from subagents import codex_runtime

    monkeypatch.setattr(effort, "resolve_effort", lambda *_: None)
    monkeypatch.setattr(codex_runtime, "ensure_connection_available", lambda: None)
    monkeypatch.setattr(codex_runtime, "has_thread_id", AsyncMock(return_value=False))
    monkeypatch.setattr(codex_runtime, "has_prior_messages", AsyncMock(return_value=False))
    feed = AsyncMock(return_value=TurnIdentity("turn-codex-exact", 1))
    monkeypatch.setattr(codex_runtime, "feed_main", feed)

    target = _target("codex", "gpt-5.6-sol", "gpt-5.6-sol")
    ctx = DispatchContext(
        thread_id="codex-main",
        thread_row={
            "model": "codex/gpt-5.6-sol",
            "project_id": None,
            "user_id": "owner",
        },
        content="continue",
        is_subagent=False,
        target=target,
        turn_id="turn-codex-exact",
        input_message_id="input-codex-exact",
        bind_input_turn=AsyncMock(),
        allow_fresh=True,
        prepared_context=prepared_context_with_fit_ledger(target),
    )
    await _accept_codex(ctx)

    assert feed.await_args.kwargs["target"] is ctx.target
    assert feed.await_args.kwargs["current_input_message_id"] == (
        "input-codex-exact"
    )
    assert "model" not in feed.await_args.kwargs
    assert "expected_reported_model" not in feed.await_args.kwargs


@pytest.mark.asyncio
async def test_codex_stale_prepared_context_is_retryable_send_rejection(monkeypatch):
    from infra.send_dispatch import dispatch_codex
    from infra.send_types import DeliveryFailureSide, SendRejected
    from subagents import codex_runtime

    monkeypatch.setattr(codex_runtime, "ensure_connection_available", lambda: None)
    monkeypatch.setattr(codex_runtime, "has_thread_id", AsyncMock(return_value=True))
    monkeypatch.setattr(
        codex_runtime,
        "feed_main",
        AsyncMock(side_effect=codex_runtime.CodexPreparedContextStale("changed")),
    )

    with pytest.raises(SendRejected) as rejected:
        await dispatch_codex(
            "codex-main",
            {"project_id": None, "user_id": "owner"},
            "next",
            "codex_main",
            None,
            target=_target("codex", "gpt-5.6-sol", "gpt-5.6-sol"),
            successor_turn_id="turn-stale-context",
            bind_input_turn=AsyncMock(),
            allow_fresh_start=True,
            prepared_context=prepared_context_with_fit_ledger(
                _target("codex", "gpt-5.6-sol", "gpt-5.6-sol"),
            ),
        )

    assert rejected.value.status_code == 409
    assert rejected.value.metadata["cause"] == "prepared_context_stale"
    assert rejected.value.side is DeliveryFailureSide.PROVIDER


@pytest.mark.asyncio
async def test_native_dispatch_forwards_the_resolved_target(monkeypatch):
    from core.turn_core import TurnIdentity
    from infra.send_dispatch import DispatchContext, _accept_native
    from infra import send_dispatch

    accepted = TurnIdentity("turn-native-exact", 1)
    dispatch_native_ws = AsyncMock(return_value=("turn_engine", accepted))
    monkeypatch.setattr(send_dispatch, "dispatch_native_ws", dispatch_native_ws)
    target = _target("native", "anthropic/claude-sonnet-4-6")
    ctx = DispatchContext(
        thread_id="native-main",
        thread_row={"model": "sonnet", "project_id": None, "user_id": "owner"},
        content="continue",
        is_subagent=False,
        target=target,
        turn_id="turn-native-exact",
        native_mode="ws",
        user={"id": "owner"},
        ws=object(),
    )

    await _accept_native(ctx)

    assert dispatch_native_ws.await_args.kwargs["target"] is target
