"""#2204: a Claude Code steer is written only while a tool reply is held.

The CLI reads queued stdin input only at a tool boundary or after a result.
A steer written during final generation is answered after result #1, which
CORSIA ignores as a pre-input result. Every other steer therefore stays
queued and becomes the successor Turn's input.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_cc_tool_result_echo_binding import _seed_thread, _tool_use

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login


def _session():
    from subagents.cc_stream import CCStreamSession

    session = CCStreamSession(binary="unused", session_id="fixture")
    session.fed, session.order, session.input_ids = [], [], []
    session.record_tool_reply = AsyncMock()
    # A measured live context keeps preflight warm, off any transcript store.
    session.tool_context.base = 100
    session.tool_context.input_measured = True

    async def feed(text, *, on_write_observed, on_accepted, **kwargs):
        session.fed.append(text)
        session.input_ids.append((kwargs.get("input_context") or (None,))[0])
        session.order.append("feed")
        on_write_observed()
        on_accepted()

    session.feed = feed
    return session


class _Registry:
    def get(self, _name):
        return None

    async def execute(self, _name: str, arguments: dict) -> str:
        return f"contents for {arguments['path']}"


def _live(identity, session):
    from subagents.cc_runtime_state import _LIVE, _Live

    live = _Live(
        session=session,
        parent_thread_id=None,
        name="cc",
        corsia_turn=identity,
        tool_registry=_Registry(),
        turn_active=True,
        last_call_usage={"input_tokens": 100},
        context_window=100_000,
        catalog_model_id="claude-code/sonnet",
    )
    _LIVE.put("thread-1", live)
    return live


def _accept_steer(message_id: str = "steer") -> None:
    from infra.input_admission import accept_thread_input

    accept_thread_input(
        "thread-1", role="user", content="steer now", intent="apply_now",
        message_id=message_id,
    )


def _row(db, message_id: str = "steer"):
    return db.execute(
        "SELECT active_turn_id,delivery_state,seq FROM messages WHERE id=?",
        (message_id,),
    ).fetchone()


async def _pump(on_event, raw: dict):
    from subagents.cc_events import normalize_event

    settlement = None
    for event in normalize_event(raw):
        settlement = await on_event(event) or settlement
    return settlement


def _result_frame() -> dict:
    return {
        "type": "result", "subtype": "success", "is_error": False,
        "result": "done", "num_turns": 1, "usage": {"input_tokens": 100},
    }


@pytest.fixture
def cc_turn(isolated_db, monkeypatch):
    from infra.database import get_db
    from subagents.cc_runtime_state import _LIVE

    monkeypatch.setattr(
        "infra.send_broadcast.broadcast_message_rows", AsyncMock(),
    )
    db = get_db()
    identity = _seed_thread(db)
    session = _session()
    live = _live(identity, session)
    try:
        yield SimpleNamespace(db=db, identity=identity, session=session, live=live)
    finally:
        _LIVE.pop("thread-1", None)
        db.close()


@pytest.mark.asyncio
async def test_cc_final_generation_steer_is_answered_by_successor_turn(cc_turn):
    from infra.send_service import request_safe_boundary
    from infra.thread_inputs import ClaimedBatch
    from subagents.cc_runtime_events import _make_on_event

    on_event = _make_on_event(cc_turn.live, "thread-1", lambda *_a: None, cc_turn.db)
    _accept_steer()
    await request_safe_boundary("thread-1", "claude-code")

    assert cc_turn.session.fed == []
    assert tuple(_row(cc_turn.db))[:2] == (None, "queued")
    settlement = await _pump(on_event, _result_frame())
    successor = getattr(settlement, "settlement", settlement).successor
    assert isinstance(successor, ClaimedBatch)
    assert [item.message_id for item in successor.items] == ["steer"]


@pytest.mark.asyncio
async def test_cc_last_tool_then_agent_send_during_final_text_specimen(cc_turn):
    """a6aea7b6 seq 132/134/135: last tool settled, agent Send, final text."""
    from api import cc_attach
    from infra.send_service import request_safe_boundary
    from infra.thread_inputs import ClaimedBatch
    from subagents.cc_runtime_events import _make_on_event

    on_event = _make_on_event(cc_turn.live, "thread-1", lambda *_a: None, cc_turn.db)
    await on_event(_tool_use("provider-last", "last.md"))
    await cc_attach._execute_admitted_tool(cc_turn.live, "thread-1", {
        "invocation_id": "invocation-last",
        "provider_tool_call_id": "provider-last",
        "name": "read_file",
        "arguments": {"path": "last.md"},
    })
    await _pump(on_event, {"type": "user", "message": {"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": "provider-last",
        "content": "contents for last.md",
    }]}})
    assert not cc_turn.live.tool_step_ids
    _accept_steer("parent-message")
    await request_safe_boundary("thread-1", "claude-code")
    assert cc_turn.session.fed == []

    settlement = await _pump(on_event, _result_frame())
    successor = getattr(settlement, "settlement", settlement).successor
    assert isinstance(successor, ClaimedBatch)
    assert [item.message_id for item in successor.items] == ["parent-message"]


@pytest.mark.asyncio
async def test_cc_tool_result_frame_does_not_write_steer(cc_turn):
    from subagents.cc_runtime_events import _make_on_event

    on_event = _make_on_event(cc_turn.live, "thread-1", lambda *_a: None, cc_turn.db)
    await on_event(_tool_use("provider-1", "a.md"))
    await cc_attach_execute(cc_turn.live, "provider-1", "a.md")
    _accept_steer()
    cc_turn.session.fed.clear()
    await _pump(on_event, {"type": "user", "message": {"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": "provider-1",
        "content": "contents for a.md",
    }]}})
    assert cc_turn.session.fed == []
    assert tuple(_row(cc_turn.db))[:2] == (None, "queued")


async def cc_attach_execute(live, call_id: str, path: str):
    from api import cc_attach

    return await cc_attach._execute_admitted_tool(live, "thread-1", {
        "invocation_id": f"invocation-{call_id}",
        "provider_tool_call_id": call_id,
        "name": "read_file",
        "arguments": {"path": path},
    })


@pytest.mark.asyncio
async def test_cc_tool_reply_writes_steer_before_reply_returns(cc_turn):
    from subagents.cc_runtime_events import _make_on_event

    on_event = _make_on_event(cc_turn.live, "thread-1", lambda *_a: None, cc_turn.db)
    await on_event(_tool_use("provider-1", "a.md"))
    _accept_steer()
    await cc_attach_execute(cc_turn.live, "provider-1", "a.md")
    cc_turn.session.order.append("reply")

    assert cc_turn.session.order == ["feed", "reply"]
    assert cc_turn.session.fed == ["steer now"]
    # The frame uuid is the durable anchor row, so a warm boundary can name it.
    assert cc_turn.session.input_ids == ["steer"]
    steer = _row(cc_turn.db)
    tool_seq = cc_turn.db.execute(
        "SELECT seq FROM messages WHERE thread_id='thread-1' AND role='tool'"
    ).fetchone()[0]
    assert tuple(steer)[:2] == (cc_turn.identity.turn_id, "delivered")
    assert steer["seq"] > tool_seq


@pytest.mark.asyncio
async def test_cc_send_during_generation_leaves_steer_queued(cc_turn):
    from infra.send_service import request_safe_boundary

    _accept_steer()
    await request_safe_boundary("thread-1", "claude-code")
    assert cc_turn.session.fed == []
    assert tuple(_row(cc_turn.db))[:2] == (None, "queued")


@pytest.mark.asyncio
async def test_cc_boundary_skips_write_after_checkpoint_resume(cc_turn, monkeypatch):
    from subagents.cc_runtime_events import _make_on_event

    monkeypatch.setattr(
        "subagents.harness_checkpoint_resume.maintain_external_tool_boundary",
        AsyncMock(return_value=True),
    )
    on_event = _make_on_event(cc_turn.live, "thread-1", lambda *_a: None, cc_turn.db)
    await on_event(_tool_use("provider-1", "a.md"))
    _accept_steer()
    await cc_attach_execute(cc_turn.live, "provider-1", "a.md")
    assert cc_turn.session.fed == []
    assert tuple(_row(cc_turn.db))[:2] == (None, "queued")


@pytest.mark.asyncio
async def test_cc_boundary_declines_while_sibling_reply_held(cc_turn):
    from subagents.cc_runtime_events import _make_on_event

    on_event = _make_on_event(cc_turn.live, "thread-1", lambda *_a: None, cc_turn.db)
    await on_event(_tool_use("provider-1", "a.md"))
    sibling = asyncio.create_task(asyncio.sleep(3600))
    cc_turn.live.admitted_tool_tasks.add(sibling)
    try:
        _accept_steer()
        await cc_attach_execute(cc_turn.live, "provider-1", "a.md")
    finally:
        cc_turn.live.admitted_tool_tasks.discard(sibling)
        sibling.cancel()
    assert cc_turn.session.fed == []
    assert tuple(_row(cc_turn.db))[:2] == (None, "queued")


def _assert_tool_not_failed(cc_turn, result, caplog, cause: str) -> None:
    """The real fence never ran: the completed tool's reply still returns."""
    assert result.delivery.content == "contents for a.md"
    assert cc_turn.live.provider_context_untrusted is False
    assert cc_turn.session._closing is False
    assert "cc held-reply steer failed thread_id=thread-1" in caplog.text
    assert cause in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "error"),
    [
        (
            "subagents.cc_runtime_steering.claim_safe_boundary",
            sqlite3.OperationalError("database is locked"),
        ),
        # The real prepare_live_input fails from inside and owns its release.
        (
            "infra.input_render._load_input_projection_async",
            RuntimeError("projection failed"),
        ),
    ],
)
async def test_cc_steer_failure_before_write_keeps_tool_result_and_queues_input(
    cc_turn, monkeypatch, caplog, target, error,
):
    from subagents.cc_runtime_events import _make_on_event

    def fail(*_args, **_kwargs):
        raise error

    async def fail_async(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(
        target, fail_async if target.endswith("_async") else fail,
    )
    on_event = _make_on_event(cc_turn.live, "thread-1", lambda *_a: None, cc_turn.db)
    await on_event(_tool_use("provider-1", "a.md"))
    _accept_steer()
    with caplog.at_level(logging.WARNING):
        result = await cc_attach_execute(cc_turn.live, "provider-1", "a.md")
    _assert_tool_not_failed(cc_turn, result, caplog, f"{type(error).__name__}: {error}")
    assert "claim changed before release" not in caplog.text
    assert cc_turn.session.fed == []
    assert tuple(_row(cc_turn.db))[:2] == (None, "queued")


@pytest.mark.asyncio
async def test_cc_begin_write_failure_after_prefix_trim_releases_each_row_once(
    cc_turn, monkeypatch, caplog,
):
    from infra import input_writes
    from subagents.cc_runtime_events import _make_on_event

    released = []
    release = input_writes.release_batch_before_write

    def recording_release(batch, **kwargs):
        released.append(tuple(item.message_id for item in batch.items))
        return release(batch, **kwargs)

    async def fits_one(prefix, *_args):
        return len(prefix.items) == 1

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(input_writes, "release_batch_before_write", recording_release)
    monkeypatch.setattr("infra.preflight_attempt.live_input_fits", fits_one)
    monkeypatch.setattr(input_writes, "begin_batch_write", locked)
    on_event = _make_on_event(cc_turn.live, "thread-1", lambda *_a: None, cc_turn.db)
    await on_event(_tool_use("provider-1", "a.md"))
    _accept_steer("steer")
    _accept_steer("steer-2")
    with caplog.at_level(logging.WARNING):
        result = await cc_attach_execute(cc_turn.live, "provider-1", "a.md")
    _assert_tool_not_failed(
        cc_turn, result, caplog, "OperationalError: database is locked",
    )
    assert sorted(released) == [("steer",), ("steer-2",)]
    assert tuple(_row(cc_turn.db, "steer"))[:2] == (None, "queued")
    assert tuple(_row(cc_turn.db, "steer-2"))[:2] == (None, "queued")


@pytest.mark.asyncio
async def test_cc_release_failure_never_replaces_the_original_cause(
    cc_turn, monkeypatch, caplog,
):
    from infra import input_writes
    from subagents.cc_runtime_events import _make_on_event

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    def release_fails(*_args, **_kwargs):
        raise RuntimeError("release also failed")

    monkeypatch.setattr(input_writes, "begin_batch_write", locked)
    monkeypatch.setattr(input_writes, "release_batch_before_write", release_fails)
    on_event = _make_on_event(cc_turn.live, "thread-1", lambda *_a: None, cc_turn.db)
    await on_event(_tool_use("provider-1", "a.md"))
    _accept_steer()
    with caplog.at_level(logging.WARNING):
        result = await cc_attach_execute(cc_turn.live, "provider-1", "a.md")
    _assert_tool_not_failed(
        cc_turn, result, caplog, "cause=OperationalError: database is locked",
    )
    assert "cc live inbox release failed thread_id=thread-1" in caplog.text
    assert "RuntimeError: release also failed" in caplog.text


@pytest.mark.asyncio
async def test_cc_stop_during_steer_preparation_propagates(cc_turn, monkeypatch):
    from subagents.cc_runtime_events import _make_on_event

    entered = asyncio.Event()

    async def blocked(*_args):
        entered.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr("infra.preflight_attempt.live_input_fits", blocked)
    on_event = _make_on_event(cc_turn.live, "thread-1", lambda *_a: None, cc_turn.db)
    await on_event(_tool_use("provider-1", "a.md"))
    _accept_steer()
    task = asyncio.create_task(cc_attach_execute(cc_turn.live, "provider-1", "a.md"))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert not cc_turn.live.admitted_tool_tasks
    assert tuple(_row(cc_turn.db))[:2] == (None, "queued")


@pytest.mark.asyncio
async def test_cc_boundary_write_failure_is_ambiguous_and_keeps_tool_result(
    cc_turn, caplog,
):
    from subagents.cc_runtime_events import _make_on_event

    async def feed(_text, *, on_write_observed, **_kwargs):
        on_write_observed()
        raise BrokenPipeError("stdin closed before drain")

    cc_turn.session.feed = feed
    on_event = _make_on_event(cc_turn.live, "thread-1", lambda *_a: None, cc_turn.db)
    await on_event(_tool_use("provider-1", "a.md"))
    _accept_steer()
    with caplog.at_level(logging.WARNING):
        result = await cc_attach_execute(cc_turn.live, "provider-1", "a.md")
    _assert_tool_not_failed(cc_turn, result, caplog, "stdin closed before drain")
    assert tuple(_row(cc_turn.db))[:2] == (cc_turn.identity.turn_id, "ambiguous")


def _assert_unsettled_write_quarantined(cc_turn, terminal) -> None:
    """The real terminal committed and left the write ambiguous, not unresolved."""
    from core.turn_core import TurnTerminalResult

    settlement = getattr(terminal, "settlement", terminal)
    result = getattr(settlement, "result", settlement)
    assert result is TurnTerminalResult.APPLIED
    assert tuple(_row(cc_turn.db))[:2] == (cc_turn.identity.turn_id, "ambiguous")
    status = cc_turn.db.execute(
        "SELECT status FROM active_turns WHERE id=?", (cc_turn.identity.turn_id,),
    ).fetchone()[0]
    assert status != "running"


@pytest.mark.asyncio
async def test_cc_accepted_settle_failure_is_quarantined_at_turn_end(
    cc_turn, monkeypatch, caplog,
):
    from subagents import cc_runtime_steering
    from subagents.cc_runtime_events import _make_on_event

    settle = cc_runtime_steering.settle_batch_write

    def locked_on_accept(attempt_id, **kwargs):
        if kwargs.get("outcome") == "accepted":
            raise sqlite3.OperationalError("database is locked")
        return settle(attempt_id, **kwargs)

    monkeypatch.setattr(cc_runtime_steering, "settle_batch_write", locked_on_accept)
    on_event = _make_on_event(cc_turn.live, "thread-1", lambda *_a: None, cc_turn.db)
    await on_event(_tool_use("provider-1", "a.md"))
    _accept_steer()
    with caplog.at_level(logging.WARNING):
        result = await cc_attach_execute(cc_turn.live, "provider-1", "a.md")
    _assert_tool_not_failed(cc_turn, result, caplog, "database is locked")
    assert cc_turn.session.fed == ["steer now"]
    await _pump(on_event, {"type": "user", "message": {"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": "provider-1",
        "content": "contents for a.md",
    }]}})
    terminal = await _pump(on_event, _result_frame())
    _assert_unsettled_write_quarantined(cc_turn, terminal)


@pytest.mark.asyncio
async def test_cc_failed_ambiguous_settle_is_quarantined_at_process_exit(
    cc_turn, monkeypatch, caplog,
):
    from subagents import cc_runtime_steering
    from subagents.cc_process_exit import finish_process_exit
    from subagents.cc_runtime_events import _make_on_event

    async def feed(_text, *, on_write_observed, **_kwargs):
        on_write_observed()
        raise BrokenPipeError("stdin closed before drain")

    async def settle_fails(*_args):
        raise sqlite3.OperationalError("database is locked")

    cc_turn.session.feed = feed
    monkeypatch.setattr(cc_runtime_steering, "_settle_failure", settle_fails)
    on_event = _make_on_event(cc_turn.live, "thread-1", lambda *_a: None, cc_turn.db)
    await on_event(_tool_use("provider-1", "a.md"))
    _accept_steer()
    with caplog.at_level(logging.WARNING):
        result = await cc_attach_execute(cc_turn.live, "provider-1", "a.md")
    _assert_tool_not_failed(cc_turn, result, caplog, "database is locked")
    # The CLI's stdin is closed, so the Turn ends by its natural process exit.
    terminal = await finish_process_exit(
        "thread-1", cc_turn.live, 1, "stdin closed", lambda *_a: None,
    )
    _assert_unsettled_write_quarantined(cc_turn, terminal)


@pytest.mark.asyncio
async def test_cc_stop_during_boundary_write_is_not_blocked(cc_turn):
    from subagents.cc_runtime_events import _make_on_event

    entered = asyncio.Event()

    async def feed(_text, *, on_write_observed, **_kwargs):
        entered.set()
        await asyncio.sleep(3600)

    cc_turn.session.feed = feed
    on_event = _make_on_event(cc_turn.live, "thread-1", lambda *_a: None, cc_turn.db)
    await on_event(_tool_use("provider-1", "a.md"))
    _accept_steer()
    task = asyncio.create_task(cc_attach_execute(cc_turn.live, "provider-1", "a.md"))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert not cc_turn.live.admitted_tool_tasks
    # No byte was observed, so the write is proved absent: the claim returns to
    # the FIFO for the successor Turn and Stop still reaches the tool task.
    assert tuple(_row(cc_turn.db))[:2] == (None, "queued")
