"""Claude Code safe-boundary contracts over the canonical message FIFO."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login


class _Registry:
    def __init__(self, live) -> None:
        self.live = live

    @asynccontextmanager
    async def lock(self, _thread_id):
        yield

    def get(self, _thread_id):
        return self.live


def _seed_boundary(db, *, intent: str = "apply_now"):
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.input_admission import accept_thread_input
    from infra.thread_inputs import start_idle_turn

    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-1','user-1','claude-code/sonnet','t0','t0')"
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
        "thread-1", role="user", content="steer now", intent=intent,
        message_id="steer", db=db,
    )
    return current


def _install_runtime(monkeypatch, steering, current, feed, *, model=None, tools=None):
    """Install a live whose calling task holds the last tool reply (#2204)."""
    from subagents.cc_stream import CCStreamSession

    session = CCStreamSession(binary="unused", session_id="fixture")
    session.feed = feed
    live = SimpleNamespace(
        corsia_turn=current.identity,
        turn_active=True,
        tool_step_ids={} if tools is None else tools,
        admitted_tool_tasks={asyncio.current_task()},
        session=session,
        catalog_model_id=model or "claude-code/sonnet",
    )
    monkeypatch.setattr(steering, "_LIVE", _Registry(live))
    broadcast = AsyncMock()
    monkeypatch.setattr(
        "infra.send_broadcast.broadcast_message_rows", broadcast,
    )
    live.broadcast = broadcast
    return live


def _steer_row(db):
    return db.execute(
        "SELECT active_turn_id,delivery_state,write_outcome,write_started_at,"
        "delivery_error FROM messages WHERE id='steer'"
    ).fetchone()


@pytest.mark.asyncio
async def test_cc_send_queues_then_held_reply_steers_the_current_turn(
    isolated_db, monkeypatch,
) -> None:
    from infra.database import get_db
    from infra.send_service import request_safe_boundary
    import subagents.cc_runtime_steering as steering

    db = get_db()
    current = _seed_boundary(db)
    db.close()
    fed = []

    async def feed(text, *, on_write_observed, on_accepted, **_kwargs):
        fed.append(text)
        on_write_observed()
        on_accepted()

    live = _install_runtime(monkeypatch, steering, current, feed)
    await request_safe_boundary("thread-1", "claude-code")
    assert fed == []
    assert await steering.deliver_live_inbox_head("thread-1") is True
    assert fed == ["steer now"]
    assert live.broadcast.await_args_list == [
        call("thread-1", ("steer",)),
        call("thread-1", ("steer",)),
    ]
    db = get_db()
    try:
        assert tuple(_steer_row(db))[:3] == ("turn-1", "delivered", "accepted")
        assert db.execute(
            "SELECT COUNT(*) FROM active_turns WHERE thread_id='thread-1'"
        ).fetchone()[0] == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_cc_outstanding_tool_defers_then_held_reply_steers_the_same_head(
    isolated_db, monkeypatch,
) -> None:
    from infra.database import get_db
    import subagents.cc_runtime_steering as steering

    db = get_db()
    current = _seed_boundary(db)
    db.close()

    async def feed(_text, *, on_write_observed, on_accepted, **_kwargs):
        on_write_observed()
        on_accepted()

    live = _install_runtime(
        monkeypatch, steering, current, feed, tools={"tool-step": object()},
    )
    assert await steering.deliver_live_inbox_head("thread-1") is False
    db = get_db()
    try:
        assert tuple(_steer_row(db))[:3] == (None, "queued", None)
    finally:
        db.close()
    live.tool_step_ids.clear()
    assert await steering.deliver_live_inbox_head("thread-1") is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("observed", "state", "outcome", "owner"),
    [
        (False, "queued", None, None),
        (True, "ambiguous", "ambiguous", "turn-1"),
    ],
)
async def test_cc_steer_settles_only_observed_provider_evidence(
    isolated_db, monkeypatch, observed, state, outcome, owner,
) -> None:
    from infra.database import get_db
    import subagents.cc_runtime_steering as steering

    db = get_db()
    current = _seed_boundary(db)
    db.close()

    async def feed(_text, *, on_write_observed, **_kwargs):
        if observed:
            on_write_observed()
        raise RuntimeError("provider refused steer")

    _install_runtime(monkeypatch, steering, current, feed)
    if observed:
        with pytest.raises(RuntimeError, match="provider refused steer"):
            await steering.deliver_live_inbox_head("thread-1")
    else:
        assert await steering.deliver_live_inbox_head("thread-1") is False
    db = get_db()
    try:
        row = _steer_row(db)
        assert tuple(row)[:3] == (owner, state, outcome)
        assert (row["write_started_at"] is not None) is observed
        if not observed:
            assert row["delivery_error"] is None
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("intent", "live_model"),
    [
        ("queue_after_turn", "claude-code/sonnet"),
        ("apply_now", "claude-code/opus"),
    ],
)
async def test_cc_incompatible_head_remains_unclaimed(
    isolated_db, monkeypatch, intent, live_model,
) -> None:
    from infra.database import get_db
    import subagents.cc_runtime_steering as steering

    db = get_db()
    current = _seed_boundary(db, intent=intent)
    db.close()

    async def feed(*_args, **_kwargs):
        raise AssertionError("incompatible input reached Claude Code")

    _install_runtime(
        monkeypatch, steering, current, feed, model=live_model,
    )
    assert await steering.deliver_live_inbox_head("thread-1") is False
    db = get_db()
    try:
        assert tuple(_steer_row(db))[:3] == (None, "queued", None)
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
                turn_active=True,
                tool_step_ids={},
                session=SimpleNamespace(),
            ),
            "no_corsia_turn",
        ),
        (
            SimpleNamespace(
                corsia_turn=SimpleNamespace(turn_id="turn-1"),
                turn_active=False,
                tool_step_ids={},
                session=SimpleNamespace(),
            ),
            "no_running_turn",
        ),
        (
            SimpleNamespace(
                corsia_turn=SimpleNamespace(turn_id="turn-1"),
                turn_active=True,
                tool_step_ids={"tool-1": object()},
                session=SimpleNamespace(),
            ),
            "not_last_held_reply steps=1 tasks=0",
        ),
        (
            SimpleNamespace(
                corsia_turn=SimpleNamespace(turn_id="turn-1"),
                turn_active=True,
                tool_step_ids={},
                session=None,
            ),
            "no_session",
        ),
    ],
)
async def test_cc_live_declines_log_the_deciding_runtime_state(
    monkeypatch, caplog, live, reason,
) -> None:
    import subagents.cc_runtime_steering as steering

    monkeypatch.setattr(steering, "_LIVE", _Registry(live))

    with caplog.at_level(logging.DEBUG, logger=steering.__name__):
        assert await steering.deliver_live_inbox_head("thread-1") is False

    assert "thread_id=thread-1" in caplog.text
    assert f"reason={reason}" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("running", "reason"),
    [(True, "no_apply_now_row"), (False, "no_running_turn")],
)
async def test_cc_empty_boundary_preserves_canonical_durable_cause_log(
    isolated_db, monkeypatch, caplog, running, reason,
) -> None:
    from infra.database import get_db
    import subagents.cc_runtime_steering as steering

    with get_db() as db:
        current = _seed_boundary(db)
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

    async def feed(*_args, **_kwargs):
        raise AssertionError("an empty safe boundary must not feed")

    _install_runtime(monkeypatch, steering, current, feed)
    with caplog.at_level(logging.DEBUG, logger="infra.thread_inputs"):
        assert await steering.deliver_live_inbox_head("thread-1") is False

    assert f"reason={reason}" in caplog.text
    assert "turn_id=turn-1" in caplog.text


@pytest.mark.asyncio
async def test_cc_model_and_provider_declines_log_deciding_values(
    isolated_db, monkeypatch, caplog,
) -> None:
    from infra.database import get_db
    import subagents.cc_runtime_steering as steering

    with get_db() as db:
        current = _seed_boundary(db)

    async def feed(_text, **_kwargs):
        return None

    live = _install_runtime(
        monkeypatch, steering, current, feed, model="claude-code/opus",
    )
    with caplog.at_level(logging.WARNING, logger=steering.__name__):
        assert await steering.deliver_live_inbox_head("thread-1") is False
    assert (
        "reason=model_mismatch batch_model=claude-code/sonnet "
        "live_model=claude-code/opus"
    ) in caplog.text

    live.catalog_model_id = "claude-code/sonnet"
    with caplog.at_level(logging.WARNING, logger=steering.__name__):
        assert await steering.deliver_live_inbox_head("thread-1") is False
    assert "reason=provider_declined accepted=False" in caplog.text
    with get_db() as db:
        assert tuple(_steer_row(db))[:3] == (None, "queued", None)


@pytest.mark.asyncio
async def test_cc_terminal_delivery_leaves_reader_free_for_control_ack() -> None:
    """A terminal may drive a switch without blocking its own set-model ack."""
    import asyncio

    from subagents.cc_stream import CCStreamSession

    entered = asyncio.Event()
    release = asyncio.Event()

    async def on_event(event: dict) -> None:
        assert event["kind"] == "result"
        entered.set()
        await release.wait()

    session = CCStreamSession(
        binary="claude",
        session_id="provider-thread",
        corsia_thread_id="thread-1",
        on_event=on_event,
    )
    await session._dispatch_raw_event({"type": "result", "is_error": False})
    await entered.wait()
    assert session._terminal_event is not None
    assert session._terminal_event.done() is False

    response = asyncio.get_running_loop().create_future()
    session._control_responses["model-1"] = response
    await session._dispatch_raw_event({
        "type": "control_response",
        "response": {"request_id": "model-1", "subtype": "success"},
    })
    assert response.result()["subtype"] == "success"

    release.set()
    await session._terminal_event
