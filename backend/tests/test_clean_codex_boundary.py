"""Codex safe-boundary contracts over the clean message FIFO."""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

from infra.thread_inputs import ClaimedBatch


def _steer_target():
    from llm.resolution import ExecutionTarget

    return ExecutionTarget(catalog_id="codex/gpt-5.6", harness="codex", provider="openai",
                           backend="codex", transport_model="gpt-5.6", context_window=200_000,
                           expected_reported_model="gpt-5.6", supports_reasoning=True,
                           available=True, availability_reason=None, display_name="Codex")


def _steer_session(steer, **kwargs):
    from core.fork_codex_context import CodexForkContext

    return SimpleNamespace(steer=steer, tool_context=CodexForkContext(_steer_target(), 100),
                           **kwargs)


class _Registry:
    def __init__(self, live) -> None:
        self.live = live

    @asynccontextmanager
    async def lock(self, _thread_id):
        yield

    def get(self, _thread_id):
        return self.live


def _seed_thread() -> ClaimedBatch:
    from infra.database import get_db
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','codex/gpt-5.6','t0','t0')"
    )
    db.commit()
    accept_thread_input(
        "thread-1", role="user", content="first", intent="apply_now",
        message_id="first", db=db,
    )
    current = start_idle_turn(
        "thread-1", turn_id="turn-1", boot_epoch="boot-1",
        started_at="t1", db=db,
    )
    assert current is not None
    begin_batch_write(current, attempt_id="first-write", started_at="t1", db=db)
    settle_batch_write("first-write", outcome="accepted", db=db)
    accept_thread_input(
        "thread-1", role="user", content="steer now", intent="apply_now",
        message_id="steer", db=db,
    )
    db.close()
    return current


@pytest.mark.asyncio
async def test_codex_send_true_steers_the_current_turn(
    isolated_db, monkeypatch,
) -> None:
    import subagents.codex_inbox_batch as steering

    current = _seed_thread()
    steered = []

    async def _steer(text, *, on_write_observed, on_drain_confirmed, **_kwargs):
        steered.append(text)
        on_write_observed()
        on_drain_confirmed()
        return True

    live = SimpleNamespace(
        corsia_turn=current.identity,
        session=_steer_session(_steer),
        catalog_model_id="codex/gpt-5.6",
    )
    monkeypatch.setattr(steering, "_LIVE", _Registry(live))
    monkeypatch.setattr(
        "llm.resolution.resolve_execution_target",
        lambda _model, **_kwargs: _steer_target(),
    )
    broadcast = AsyncMock()
    monkeypatch.setattr(
        "infra.send_broadcast.broadcast_message_rows", broadcast,
    )
    monkeypatch.setattr(steering, "_begin_event_buffer", lambda _live: None)

    async def _release(_live):
        return None

    monkeypatch.setattr(steering, "_release_event_buffer", _release)

    assert await steering.deliver_live_inbox_batch("thread-1") is True
    assert steered == ["steer now"]
    assert broadcast.await_args_list == [
        call("thread-1", ("steer",)),
        call("thread-1", ("steer",)),
    ]

    from infra.database import get_db

    db = get_db()
    try:
        assert tuple(db.execute(
            "SELECT active_turn_id,delivery_state,write_outcome "
            "FROM messages WHERE id='steer'"
        ).fetchone()) == ("turn-1", "delivered", "accepted")
        assert db.execute(
            "SELECT COUNT(*) FROM active_turns WHERE thread_id='thread-1'"
        ).fetchone()[0] == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_codex_unknown_post_write_failure_is_visible_and_not_replayed(
    isolated_db, monkeypatch,
) -> None:
    import subagents.codex_inbox_batch as steering

    current = _seed_thread()

    async def _steer(_text, *, on_write_observed, **_kwargs):
        on_write_observed()
        raise TimeoutError("provider acknowledgement was lost")

    live = SimpleNamespace(
        corsia_turn=current.identity,
        session=_steer_session(_steer),
        catalog_model_id="codex/gpt-5.6",
    )
    monkeypatch.setattr(steering, "_LIVE", _Registry(live))
    monkeypatch.setattr(
        "llm.resolution.resolve_execution_target",
        lambda _model, **_kwargs: _steer_target(),
    )
    monkeypatch.setattr(
        "infra.send_broadcast.broadcast_message_rows", AsyncMock(),
    )
    monkeypatch.setattr(steering, "_begin_event_buffer", lambda _live: None)

    async def _release(_live):
        return None

    monkeypatch.setattr(steering, "_release_event_buffer", _release)

    with pytest.raises(TimeoutError):
        await steering.deliver_live_inbox_batch("thread-1")

    from infra.database import get_db

    db = get_db()
    try:
        assert tuple(db.execute(
            "SELECT delivery_state,write_outcome FROM messages WHERE id='steer'"
        ).fetchone()) == ("ambiguous", "ambiguous")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_codex_proved_unwritten_steer_stays_queued_without_public_error(
    isolated_db, monkeypatch,
) -> None:
    """A pre-write transport failure is a successful queued-send receipt."""
    import subagents.codex_inbox_batch as steering

    current = _seed_thread()

    async def _steer(_text, **_kwargs):
        raise RuntimeError("transport was already unavailable")

    live = SimpleNamespace(
        corsia_turn=current.identity,
        session=_steer_session(_steer),
        catalog_model_id="codex/gpt-5.6",
    )
    monkeypatch.setattr(steering, "_LIVE", _Registry(live))
    monkeypatch.setattr(
        "llm.resolution.resolve_execution_target",
        lambda _model, **_kwargs: _steer_target(),
    )
    monkeypatch.setattr(
        "infra.send_broadcast.broadcast_message_rows", AsyncMock(),
    )
    monkeypatch.setattr(steering, "_begin_event_buffer", lambda _live: None)

    async def _release(_live):
        return None

    monkeypatch.setattr(steering, "_release_event_buffer", _release)

    assert await steering.deliver_live_inbox_batch("thread-1") is False

    from infra.database import get_db

    db = get_db()
    try:
        assert tuple(db.execute(
            "SELECT active_turn_id,delivery_state,write_outcome,"
            "write_started_at,delivery_error FROM messages WHERE id='steer'"
        ).fetchone()) == (None, "queued", None, None, None)
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("live", "reason"),
    [
        (None, "no_live_handle"),
        (
            SimpleNamespace(
                corsia_turn=None,
                session=SimpleNamespace(returncode=None),
            ),
            "no_corsia_turn",
        ),
        (
            SimpleNamespace(
                corsia_turn=SimpleNamespace(turn_id="turn-1"),
                session=None,
            ),
            "no_session",
        ),
        (
            SimpleNamespace(
                corsia_turn=SimpleNamespace(turn_id="turn-1"),
                session=SimpleNamespace(returncode=None),
                tool_step_ids={"tool-1": object()},
            ),
            "outstanding_tool_steps count=1",
        ),
    ],
)
async def test_codex_live_declines_log_the_deciding_runtime_state(
    monkeypatch, caplog, live, reason,
) -> None:
    import subagents.codex_inbox_batch as steering

    monkeypatch.setattr(steering, "_LIVE", _Registry(live))

    with caplog.at_level(logging.DEBUG, logger=steering.__name__):
        assert await steering.deliver_live_inbox_batch("thread-1") is False

    assert "thread_id=thread-1" in caplog.text
    assert f"reason={reason}" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("running", "reason"),
    [(True, "no_apply_now_row"), (False, "no_running_turn")],
)
async def test_codex_empty_boundary_preserves_canonical_durable_cause_log(
    isolated_db, monkeypatch, caplog, running, reason,
) -> None:
    from infra.database import get_db
    import subagents.codex_inbox_batch as steering

    current = _seed_thread()
    with get_db() as db:
        if running:
            db.execute(
                "UPDATE messages SET delivery_intent='queue_after_turn' "
                "WHERE id='steer'",
            )
        else:
            db.execute(
                "UPDATE active_turns SET status='completed' WHERE id='turn-1'",
            )
        db.commit()
    live = SimpleNamespace(
        corsia_turn=current.identity,
        session=SimpleNamespace(returncode=None),
        catalog_model_id="codex/gpt-5.6",
    )
    monkeypatch.setattr(steering, "_LIVE", _Registry(live))

    with caplog.at_level(logging.DEBUG, logger="infra.thread_inputs"):
        assert await steering.deliver_live_inbox_batch("thread-1") is False

    assert f"reason={reason}" in caplog.text
    assert "turn_id=turn-1" in caplog.text


@pytest.mark.asyncio
async def test_codex_model_and_provider_declines_log_deciding_values(
    isolated_db, monkeypatch, caplog,
) -> None:
    import subagents.codex_inbox_batch as steering

    current = _seed_thread()

    async def steer(_text, **_kwargs):
        return False

    live = SimpleNamespace(
        corsia_turn=current.identity,
        session=_steer_session(steer, returncode=None),
        catalog_model_id="codex/gpt-5.6-other",
    )
    monkeypatch.setattr(steering, "_LIVE", _Registry(live))
    monkeypatch.setattr(
        "llm.resolution.resolve_execution_target",
        lambda _model, **_kwargs: _steer_target(),
    )
    monkeypatch.setattr(
        "infra.send_broadcast.broadcast_message_rows", AsyncMock(),
    )
    monkeypatch.setattr(steering, "_begin_event_buffer", lambda _live: None)

    async def release(_live):
        return None

    monkeypatch.setattr(steering, "_release_event_buffer", release)
    with caplog.at_level(logging.WARNING, logger=steering.__name__):
        assert await steering.deliver_live_inbox_batch("thread-1") is False
    assert (
        "reason=model_mismatch batch_model=codex/gpt-5.6 "
        "live_model=codex/gpt-5.6-other"
    ) in caplog.text

    live.catalog_model_id = "codex/gpt-5.6"
    with caplog.at_level(logging.WARNING, logger=steering.__name__):
        assert await steering.deliver_live_inbox_batch("thread-1") is False
    assert "reason=provider_declined accepted=False" in caplog.text


@pytest.mark.asyncio
async def test_codex_dead_live_decline_logs_returncode(monkeypatch, caplog) -> None:
    import subagents.codex_inbox_batch as steering

    async def settle(_thread_id):
        return None

    live = SimpleNamespace(
        corsia_turn=SimpleNamespace(turn_id="turn-1"),
        session=SimpleNamespace(returncode=-9),
    )
    monkeypatch.setattr(steering, "_LIVE", _Registry(live))
    monkeypatch.setattr(
        "subagents.codex_runtime.settle_dead_live_generation_if_observed",
        settle,
    )

    with caplog.at_level(logging.DEBUG, logger=steering.__name__):
        assert await steering.deliver_live_inbox_batch("thread-1") is False

    assert "reason=dead_live_session returncode=-9" in caplog.text


@pytest.mark.asyncio
async def test_codex_terminal_delivery_leaves_json_rpc_reader_free(
    monkeypatch,
) -> None:
    """A terminal may start its successor without blocking turn/start's reply."""
    import asyncio

    from subagents import codex_runtime_event_delivery as delivery
    from subagents.codex_runtime_state import _LiveCodex

    entered = asyncio.Event()
    release = asyncio.Event()

    async def inner(event: dict) -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr(
        delivery.codex_runtime_events,
        "make_on_event",
        lambda *_args, **_kwargs: inner,
    )
    live = _LiveCodex(
        codex_thread_id="provider-thread",
        session=SimpleNamespace(),
        parent_thread_id=None,
        name="codex",
    )
    live.delivery_event_gate = asyncio.Event()
    live.delivery_event_gate.set()
    on_event = delivery.runtime_event_handler(live, "thread-1", lambda *_: None)

    await on_event({"kind": "result", "is_current_turn": True})
    await entered.wait()
    assert live.delivery_event_tail is not None
    assert live.delivery_event_tail.done() is False

    release.set()
    await live.delivery_event_tail


@pytest.mark.asyncio
async def test_codex_tool_boundary_updates_widget_with_exact_handle_identity(
    monkeypatch,
) -> None:
    from core.turn_core import TurnIdentity
    from subagents import codex_runtime_event_delivery as delivery
    from subagents.codex_runtime_state import _LiveCodex

    session = SimpleNamespace(
        transport_generation_id="11111111-1111-4111-8111-111111111111",
        returncode=None,
        working_dir="/tmp",
        peek_active_usage=lambda: {
            "input_tokens": 42_000,
            "cached_input_tokens": 40_000,
            "model_context_window": 258_400,
        },
    )
    live = _LiveCodex(
        codex_thread_id="provider-thread",
        session=session,
        parent_thread_id=None,
        name="codex",
        model="gpt-5.6-luna",
        catalog_model_id="codex/gpt-5.6-luna",
        runtime_context_epoch=5,
        corsia_turn=TurnIdentity("turn-codex-usage", 6),
    )
    monkeypatch.setattr(
        delivery.codex_runtime_events,
        "make_on_event",
        lambda *_args, **_kwargs: AsyncMock(),
    )
    monkeypatch.setattr(delivery, "_schedule_safe_boundary", lambda _sid: None)
    casts: list[tuple[str, dict]] = []
    on_event = delivery.runtime_event_handler(
        live, "thread-1", lambda *args: casts.append(args),
    )

    await on_event({"kind": "tool_result"})

    assert len(casts) == 1
    thread_id, event = casts[0]
    assert thread_id == "thread-1"
    assert event["type"] == "context_usage_updated"
    assert event["turn_id"] == "turn-codex-usage"
    assert event["turn_order"] == 6
    assert event["context_usage"]["total_used"] == 42_000
    assert "harness" not in event["context_usage"]
    assert "catalog_model_id" not in event["context_usage"]
    assert "runtime_context_epoch" not in event["context_usage"]
    assert "producer_handle_fingerprint" not in event["context_usage"]


@pytest.mark.asyncio
async def test_codex_queued_tool_boundary_keeps_ingress_usage_and_turn(
    monkeypatch,
) -> None:
    """Queued delivery cannot relabel a boundary with successor state."""
    import asyncio

    from core.turn_core import TurnIdentity
    from subagents import codex_runtime_event_delivery as delivery
    from subagents.codex_runtime_state import _LiveCodex

    old_usage = {
        "input_tokens": 42_000,
        "cached_input_tokens": 40_000,
        "model_context_window": 258_400,
    }
    usage = old_usage
    session = SimpleNamespace(
        transport_generation_id="11111111-1111-4111-8111-111111111111",
        returncode=None,
        working_dir="/tmp",
        peek_active_usage=lambda: usage,
    )
    old_turn = TurnIdentity("turn-old", 6)
    live = _LiveCodex(
        codex_thread_id="provider-thread",
        session=session,
        parent_thread_id=None,
        name="codex",
        model="gpt-5.6-luna",
        catalog_model_id="codex/gpt-5.6-luna",
        runtime_context_epoch=5,
        corsia_turn=old_turn,
    )
    monkeypatch.setattr(
        delivery.codex_runtime_events,
        "make_on_event",
        lambda *_args, **_kwargs: AsyncMock(),
    )
    monkeypatch.setattr(delivery, "_schedule_safe_boundary", lambda _sid: None)
    release = asyncio.Event()
    prior = asyncio.create_task(release.wait())
    live.delivery_event_gate = asyncio.Event()
    live.delivery_event_gate.set()
    live.delivery_event_tail = prior
    casts: list[tuple[str, dict]] = []
    on_event = delivery.runtime_event_handler(
        live, "thread-1", lambda *args: casts.append(args),
    )

    queued = asyncio.create_task(on_event({"kind": "tool_result"}))
    while live.delivery_event_tail is prior:
        await asyncio.sleep(0)
    live.corsia_turn = TurnIdentity("turn-successor", 7)
    usage = {
        "input_tokens": 99_000,
        "cached_input_tokens": 1_000,
        "model_context_window": 258_400,
    }
    release.set()
    await queued

    assert len(casts) == 1
    _, event = casts[0]
    assert event["turn_id"] == old_turn.turn_id
    assert event["turn_order"] == old_turn.turn_order
    assert event["context_usage"]["total_used"] == old_usage["input_tokens"]


@pytest.mark.asyncio
async def test_codex_queued_event_keeps_ingress_turn_after_live_retires(
    monkeypatch,
) -> None:
    """Deferred provider output must not inherit mutable successor state."""
    import asyncio

    from core.turn_core import TurnIdentity
    from subagents import codex_runtime_event_delivery as delivery
    from subagents.codex_runtime_state import _LiveCodex

    old_turn = TurnIdentity("turn-old", 6)
    live = _LiveCodex(
        codex_thread_id="provider-thread",
        session=SimpleNamespace(),
        parent_thread_id=None,
        name="codex",
        corsia_turn=old_turn,
    )
    delivered: list[dict] = []

    async def inner(event: dict) -> None:
        delivered.append(event)

    monkeypatch.setattr(
        delivery.codex_runtime_events,
        "make_on_event",
        lambda *_args, **_kwargs: inner,
    )
    release = asyncio.Event()
    prior = asyncio.create_task(release.wait())
    live.delivery_event_gate = asyncio.Event()
    live.delivery_event_gate.set()
    live.delivery_event_tail = prior
    on_event = delivery.runtime_event_handler(
        live, "thread-1", lambda *_args: None,
    )

    queued = asyncio.create_task(on_event({"kind": "step_reasoning", "text": "old"}))
    while live.delivery_event_tail is prior:
        await asyncio.sleep(0)
    live.corsia_turn = None
    release.set()
    await queued

    assert delivered[0]["_corsia_turn_identity"] == old_turn


def test_codex_live_usage_merges_snapshot_window_without_changing_provider_truth() -> None:
    from core.turn_core import TurnIdentity
    from subagents.codex_runtime_state import _LiveCodex
    from subagents.codex_runtime_terminal import live_measured_context_usage

    session = SimpleNamespace(
        transport_generation_id="11111111-1111-4111-8111-111111111111",
        returncode=None,
        working_dir="/tmp",
        peek_active_usage=lambda: {"input_tokens": 42_000},
    )
    live = _LiveCodex(
        codex_thread_id="provider-thread",
        session=session,
        parent_thread_id=None,
        name="codex",
        model="gpt-5.6-luna",
        catalog_model_id="codex/gpt-5.6-luna",
        runtime_context_epoch=5,
        corsia_turn=TurnIdentity("turn-codex-usage", 6),
        context_snapshot={
            "harness_model_facts": {"model_context_window": 258_400},
        },
    )
    receipt = live_measured_context_usage("thread-1", live)

    assert receipt is not None
    assert receipt["total_used"] == 42_000
    assert receipt["model_context_window"] == 258_400
    assert receipt["utilization"] == 0.163
    assert receipt["provider_reported_utilization"] is None


def test_codex_live_usage_requires_the_app_server_usage_contract() -> None:
    from subagents.codex_runtime_state import _LiveCodex
    from subagents.codex_runtime_terminal import live_measured_context_usage

    live = _LiveCodex(
        session=SimpleNamespace(),
        parent_thread_id=None,
        name="codex",
    )

    with pytest.raises(AttributeError, match="peek_active_usage"):
        live_measured_context_usage("thread-1", live)


@pytest.mark.asyncio
async def test_codex_serialized_event_reports_prior_failure_and_continues(
    caplog,
) -> None:
    """A later provider event may continue, but never hides its predecessor."""
    import asyncio
    import logging

    from subagents import codex_runtime_event_delivery as delivery

    async def fail() -> None:
        raise RuntimeError("prior event failed")

    prior = asyncio.create_task(fail())
    with pytest.raises(RuntimeError, match="prior event failed"):
        await prior
    deliver = AsyncMock()

    with caplog.at_level(logging.WARNING, logger=delivery.__name__):
        await delivery._deliver_serialized(
            SimpleNamespace(),
            "thread-1",
            {"kind": "text_delta"},
            deliver,
            gate=None,
            previous=prior,
            epoch=0,
        )

    deliver.assert_awaited_once_with({"kind": "text_delta"})
    assert "continued after failed prior provider event" in caplog.text
