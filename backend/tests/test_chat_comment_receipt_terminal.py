"""Terminal receipt facts use the same persisted turn classification as Focus."""

import pytest

from infra.chat_comment_receipts import terminal_comment_only_receipt
from test_chat_comment_receipt_focus import _comment_turn
from infra.chat_comment_schema import CHAT_COMMENT_SCHEMA_SQL
from test_thread_focus_page import _SID, _Thread, _db


@pytest.mark.asyncio
async def test_terminal_receipt_uses_persisted_turn_and_output():
    db = _db()
    db.executescript(CHAT_COMMENT_SCHEMA_SQL)
    thread = _Thread(db)
    _comment_turn(db, thread, "comment")
    assert await terminal_comment_only_receipt(_SID, "comment", db=db) is True
    db.execute("UPDATE chat_visible_content SET content='Chat answer'")
    assert await terminal_comment_only_receipt(_SID, "comment", db=db) is False


@pytest.mark.asyncio
async def test_native_terminal_carries_comment_only_fact(monkeypatch):
    import core.step_handler as steps

    frames = []

    async def broadcast(frame):
        frames.append(frame)

    async def classify(thread_id, turn_id):
        assert (thread_id, turn_id) == (_SID, "comment")
        return True

    async def ignore(*args, **kwargs):
        return None

    monkeypatch.setattr(steps, "terminal_comment_only_receipt", classify)
    monkeypatch.setattr(steps, "run_in_db", ignore)
    monkeypatch.setattr(steps, "record_turn_end_logged", ignore)
    handler = steps.StepHandler(_SID, {}, broadcast, "comment")
    handler._emit = broadcast
    await handler._handle_turn_complete({})
    assert frames[0]["comment_only_receipt"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(("is_error", "expected"), [(False, True), (True, None)])
async def test_claude_code_terminal_carries_fact_only_when_completed(monkeypatch, is_error, expected):
    from unittest.mock import AsyncMock

    from core.turn_core import TurnIdentity, TurnTerminalResult
    from infra.thread_driver import TerminalSettlement
    from subagents import cc_event_result
    from subagents.cc_event_context import CCEventContext
    from subagents.cc_runtime_state import _Live

    identity = TurnIdentity("comment", 1)
    live = _Live(session=None, parent_thread_id=None, name="cc", corsia_turn=identity)
    live.turn_active = True
    classify = AsyncMock(return_value=True)
    finish = AsyncMock()
    monkeypatch.setattr(cc_event_result, "terminal_comment_only_receipt", classify)
    monkeypatch.setattr(cc_event_result, "claim_terminal", AsyncMock(
        return_value=TerminalSettlement(TurnTerminalResult.APPLIED, None)))
    monkeypatch.setattr(cc_event_result, "_finish_result_postprocessing", finish)
    monkeypatch.setattr(cc_event_result, "_insert_result_marker", AsyncMock(return_value=("marker", "now")))
    monkeypatch.setattr(cc_event_result, "_announce_result_marker", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cc_event_result, "_record_plan_error", AsyncMock())
    monkeypatch.setattr(cc_event_result, "_record_plan_completion", AsyncMock())
    monkeypatch.setattr(cc_event_result, "_schedule_sparse_usage_control_read", lambda _ctx: None)
    await cc_event_result.on_result(CCEventContext(live, _SID, lambda *_args: None, None), {
        "_corsia_turn_identity": identity, "is_error": is_error, "result": "done",
    })
    assert finish.await_args.args[1].get("comment_only_receipt") is expected
    assert classify.await_count == (0 if is_error else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(("terminal_type", "expected"), [("turn_complete", True), ("turn_error", None)])
async def test_codex_terminal_carries_fact_only_when_completed(monkeypatch, terminal_type, expected):
    from unittest.mock import AsyncMock

    from core.turn_core import TurnIdentity
    from subagents import codex_runtime_terminal
    from subagents.codex_runtime_state import _LiveCodex

    identity = TurnIdentity("comment", 1)
    live = _LiveCodex(session=None, parent_thread_id=None, name="codex", corsia_turn=identity)
    classify = AsyncMock(return_value=True)
    sent = []
    monkeypatch.setattr(codex_runtime_terminal, "terminal_comment_only_receipt", classify)
    monkeypatch.setattr(codex_runtime_terminal, "_attach_terminal_context", AsyncMock())
    monkeypatch.setattr(codex_runtime_terminal, "_observe_terminal_context", lambda *_args, **_kwargs: None)
    await codex_runtime_terminal.publish_claimed_terminal(
        thread_id=_SID, live=live, identity=identity, bcast=lambda _thread, frame: sent.append(frame),
        source="test", usage=None, terminal_type=terminal_type,
    )
    assert sent[0].get("comment_only_receipt") is expected
    assert classify.await_count == (1 if terminal_type == "turn_complete" else 0)
