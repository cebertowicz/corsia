"""Initial hard-boundary cancellation and conservative accounting proofs."""

from __future__ import annotations

from llm.context_window import WindowFacts
import asyncio
from types import SimpleNamespace

import pytest


def test_context_snapshot_router_keeps_only_next_turn_preview() -> None:
    from api.thread_runtime import router

    paths = {
        route.path for route in router.routes if hasattr(route, "path")
    }

    assert "/api/threads/{thread_id}/context-snapshot/next-turn" in paths
    assert "/api/threads/{thread_id}/context-snapshot/latest" not in paths
    assert "/api/threads/{thread_id}/context-snapshot/{record_id}" not in paths


def test_warm_capacity_uses_terminal_receipt_plus_exact_tail(
    isolated_db,
    monkeypatch,
) -> None:
    from core.token_estimation import _estimate_tokens
    from infra import send_replay_reads
    from infra import context_receipts
    from infra.context_receipts import ContextReceiptIdentity, attach_context_receipt
    from infra.database import get_db
    from infra.queries import insert_message
    from shared_send_fixtures import _insert_session

    _insert_session("thread-receipt", model="claude-code/sonnet", spawned_by=None)
    identity = ContextReceiptIdentity(
        "claude-code",
        "claude-code/sonnet",
        0,
        "a" * 64,
    )
    db = get_db()
    try:
        db.execute(
            "INSERT INTO active_turns "
            "(id,thread_id,execution_epoch,model,status,started_at,ended_at,boot_epoch) "
            "VALUES ('turn-receipt','thread-receipt',0,'claude-code/sonnet',"
            "'completed','start','end','boot')"
        )
        insert_message(
            "thread-receipt",
            "assistant",
            "done",
            {"turn_id": "turn-receipt"},
            msg_id="assistant-receipt",
            db=db,
        )
        insert_message(
            "thread-receipt",
            "tool",
            "tool result",
            {
                "turn_id": "turn-receipt",
                "assistant_step_id": "assistant-receipt",
            },
            msg_id="tool-receipt",
            db=db,
        )
        attach_context_receipt(
            "thread-receipt",
            "assistant-receipt",
            context_usage={
                "model_context_window": 200_000,
                "total_used": 90_000,
                "token_usage_available": True,
                "token_source": "measured",
            },
            identity=identity,
            db=db,
        )
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(
        context_receipts,
        "current_context_receipt_identity",
        lambda *_args, **_kwargs: identity,
    )
    monkeypatch.setattr(
        "llm.resolution.resolve_execution_target",
        lambda *_args, **_kwargs: SimpleNamespace(
            harness="claude-code",
            catalog_id="claude-code/sonnet",
            transport_model="sonnet",
            expected_reported_model=None,
        ),
    )

    capacity = send_replay_reads.read_provider_capacity("thread-receipt")

    assert capacity.measured_tokens == 90_000
    assert capacity.tail_tokens == (
        _estimate_tokens("done") + 4 + _estimate_tokens("tool result") + 4
    )


def test_unavailable_capacity_target_falls_back_to_exact_render(monkeypatch) -> None:
    """Missing telemetry resolution is not itself a send failure."""
    from infra import send_replay_reads
    from llm.resolution import ModelUnavailableError

    class _Db:
        closed = False

        def execute(self, *_args, **_kwargs):
            return SimpleNamespace(
                fetchone=lambda: {
                    "model": "claude-code/missing",
                    "runtime_context_epoch": 1,
                }
            )

        def close(self) -> None:
            self.closed = True

    db = _Db()
    monkeypatch.setattr("infra.database.get_db", lambda: db)
    monkeypatch.setattr(
        "llm.resolution.resolve_execution_target",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ModelUnavailableError("claude-code/missing", "not discovered")
        ),
    )

    assert send_replay_reads.read_provider_capacity("thread-1") is None
    assert db.closed is True


@pytest.mark.asyncio
async def test_cc_cancelled_boundary_cannot_write_an_error_terminal(
    monkeypatch,
) -> None:
    """Stop cancellation unwinds before CC's provider-write error handling."""
    from core.checkpoint_resume import CheckpointContinuationError
    from core.checkpoint_wait import CheckpointWaitScope
    from core.turn_core import TurnIdentity
    from subagents import cc_accept

    identity = TurnIdentity("turn-1", 1)
    entered = asyncio.Event()
    release = asyncio.Event()
    error_terminals = []
    scope = CheckpointWaitScope()

    async def admit(*_args, **_kwargs):
        return identity

    async def boundary(*_args):
        entered.set()
        await release.wait()
        scope.request_stop()
        raise CheckpointContinuationError("checkpoint cancelled by Stop")

    async def report(*_args):
        error_terminals.append(True)

    monkeypatch.setattr(cc_accept, "_admit_turn", admit)
    monkeypatch.setattr(cc_accept, "_report_prewrite_failure", report)
    task = asyncio.create_task(cc_accept.accept_reconstructed_input(
        "thread-1",
        SimpleNamespace(checkpoint_wait_scope=scope),
        object(),
        turn_id=identity.turn_id,
        bcast=lambda *_args: None,
        before_provider_write=boundary,
    ))
    await entered.wait()
    release.set()

    with pytest.raises(CheckpointContinuationError):
        await task
    assert error_terminals == []


@pytest.mark.asyncio
async def test_cc_prewrite_failure_settles_outer_fence_before_terminal(
    monkeypatch,
) -> None:
    """An ordinary CC send preserves its causal error after write settlement."""
    from core.checkpoint_wait import CheckpointWaitScope
    from core.turn_core import TurnIdentity
    from subagents import cc_accept

    identity = TurnIdentity("turn-1", 1)
    events: list[object] = []

    class _Session:
        async def feed(self, *_args, **_kwargs):
            raise RuntimeError("original CC startup failure")

    live = SimpleNamespace(
        checkpoint_wait_scope=CheckpointWaitScope(),
        event_lock=asyncio.Lock(),
        session=_Session(),
    )

    async def admit(*_args, **_kwargs):
        return identity

    async def settle_terminal(outcome, detail):
        assert live.event_lock.locked()
        events.append(("settled", outcome, detail))

    async def report(*_args):
        events.append("terminal")

    monkeypatch.setattr(cc_accept, "_admit_turn", admit)
    monkeypatch.setattr(cc_accept, "_prepare_live_turn", lambda *_args: None)
    monkeypatch.setattr(cc_accept, "_report_prewrite_failure", report)

    with pytest.raises(RuntimeError, match="original CC startup failure"):
        await cc_accept.accept_reconstructed_input(
            "thread-1",
            live,
            SimpleNamespace(turn_text="input"),
            turn_id=identity.turn_id,
            bcast=lambda *_args: None,
            settle_delivery_terminal=settle_terminal,
        )

    assert events == [
        ("settled", "error", "original CC startup failure"),
        "terminal",
    ]


@pytest.mark.asyncio
async def test_cc_prewrite_cancellation_keeps_turn_cancelled(monkeypatch) -> None:
    """A pre-write Stop cannot disagree with its input's cancelled outcome."""
    from core.turn_core import TerminalSettlement, TurnIdentity, TurnTerminalResult
    from subagents.cc_process_exit import _CommittedTerminalFailure
    from subagents import cc_accept

    identity = TurnIdentity("turn-1", 1)
    live = SimpleNamespace(
        turn_terminal=None, corsia_turn=identity, turn_active=True,
    )
    outcomes: list[tuple[str, str]] = []

    async def claim(_live, outcome, detail, *, identity, defer_drive=False):
        assert defer_drive is True
        outcomes.append((outcome, detail))
        return TerminalSettlement(TurnTerminalResult.ALREADY_SAME_OUTCOME, "thread-1")

    monkeypatch.setattr(cc_accept, "claim_terminal", claim)
    with pytest.raises(_CommittedTerminalFailure) as failure:
        await cc_accept._report_prewrite_failure(
            "thread-1", live, identity, asyncio.CancelledError(),
            lambda *_args: None,
        )
    assert isinstance(failure.value.publication_error, asyncio.CancelledError)

    assert outcomes == [("cancelled", "")]


@pytest.mark.asyncio
async def test_codex_stop_owned_boundary_failure_preserves_cancel_owner(
    monkeypatch,
) -> None:
    """Codex spawn cleanup cannot replace Stop's cancelled terminal with error."""
    from codex_runtime_fixtures import locked_spawn
    from subagents import codex_runtime
    from core.turn_core import TurnIdentity
    from subagents.codex_runtime_state import _APP_SESSIONS, _LIVE, _LiveCodex

    session = SimpleNamespace(returncode=None)
    live = _LiveCodex(session, None, "codex")
    live.provider_context_untrusted = True
    live.checkpoint_wait_scope.request_stop()
    identity = TurnIdentity("turn-1", 1)
    live.corsia_turn = identity
    _LIVE.put("thread-1", live)
    _APP_SESSIONS["thread-1"] = session
    input_outcomes = []

    async def start(*_args, **_kwargs):
        return identity

    async def configure(*_args, **_kwargs):
        return session, None

    async def boundary(*_args):
        raise RuntimeError("checkpoint cancelled by Stop")

    async def settle(outcome, _error):
        input_outcomes.append(outcome)

    async def reject(*_args, **_kwargs):
        raise AssertionError("Stop-owned Turn was rewritten as an error")

    monkeypatch.setattr(codex_runtime, "_new_app_server_live", lambda *_a, **_k: live)
    monkeypatch.setattr(codex_runtime, "_reusable_app_session", lambda _id: session)
    monkeypatch.setattr(codex_runtime, "_resolve_app_server_tools", lambda *_a, **_k: (object(), ()))
    monkeypatch.setattr(codex_runtime, "_start_live_turn", start)
    monkeypatch.setattr(codex_runtime, "_configure_spec_app_server_input", configure)
    monkeypatch.setattr(codex_runtime, "_maintain_codex_initial_boundary", boundary)
    monkeypatch.setattr(codex_runtime, "_finish_failed_app_server_spawn", reject)
    try:
        with pytest.raises(RuntimeError, match="checkpoint cancelled by Stop"):
            await locked_spawn(
                codex_runtime._spawn_app_server_turn,
                "thread-1", "input", current_user_text="input",
                successor_turn_id=identity.turn_id, codex_thread_id=None,
                binary="codex", working_dir=None, windows=WindowFacts(128_000, None),
                preadmitted_turn=identity, settle_delivery_terminal=settle,
                broadcast=lambda *_args: None,
            )
        assert input_outcomes == ["cancelled"]
        assert _LIVE.get("thread-1") is live
        assert _APP_SESSIONS["thread-1"] is session
    finally:
        _LIVE.pop("thread-1", None)
        _APP_SESSIONS.pop("thread-1", None)
