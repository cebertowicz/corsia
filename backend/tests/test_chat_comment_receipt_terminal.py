"""Terminal receipt facts use the same persisted turn classification as Focus."""

import pytest

from infra.chat_comment_receipts import comment_only_receipt_field
from test_chat_comment_receipt_focus import _comment_turn
from infra.chat_comment_schema import CHAT_COMMENT_SCHEMA_SQL
from test_thread_focus_page import _SID, _Thread, _db

_FACT = {"comment_only_receipt": True}


def _turn_db(status="completed"):
    db = _db()
    db.executescript(CHAT_COMMENT_SCHEMA_SQL)
    _comment_turn(db, _Thread(db), "comment", status=status)
    return db


@pytest.mark.asyncio
async def test_terminal_receipt_uses_persisted_turn_and_output():
    db = _turn_db()
    assert await comment_only_receipt_field(_SID, "comment", db=db) == _FACT
    db.execute("UPDATE chat_visible_content SET content='Chat answer'")
    assert await comment_only_receipt_field(_SID, "comment", db=db) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["error", "cancelled", "running"])
async def test_terminal_receipt_needs_completed_turn_status(status):
    assert await comment_only_receipt_field(_SID, "comment", db=_turn_db(status)) == {}


@pytest.mark.asyncio
async def test_native_terminal_carries_comment_only_fact(monkeypatch):
    import core.step_handler as steps

    frames = []

    async def broadcast(frame):
        frames.append(frame)

    async def classify(thread_id, turn_id):
        assert (thread_id, turn_id) == (_SID, "comment")
        return _FACT

    async def ignore(*args, **kwargs):
        return None

    monkeypatch.setattr(steps, "comment_only_receipt_field", classify)
    monkeypatch.setattr(steps, "run_in_db", ignore)
    monkeypatch.setattr(steps, "record_turn_end_logged", ignore)
    handler = steps.StepHandler(_SID, {}, broadcast, "comment")
    handler._emit = broadcast
    await handler._handle_turn_complete({})
    assert frames[0]["comment_only_receipt"] is True


@pytest.mark.asyncio
async def test_claude_code_result_hands_its_turn_to_postprocessing(monkeypatch):
    from unittest.mock import AsyncMock

    from core.turn_core import TurnIdentity, TurnTerminalResult
    from infra.thread_driver import TerminalSettlement
    from subagents import cc_event_result
    from subagents.cc_event_context import CCEventContext
    from subagents.cc_runtime_state import _Live

    identity = TurnIdentity("comment", 1)
    live = _Live(session=None, parent_thread_id=None, name="cc", corsia_turn=identity)
    live.turn_active = True
    finish = AsyncMock()
    monkeypatch.setattr(cc_event_result, "claim_terminal", AsyncMock(
        return_value=TerminalSettlement(TurnTerminalResult.APPLIED, None)))
    monkeypatch.setattr(cc_event_result, "_finish_result_postprocessing", finish)
    monkeypatch.setattr(cc_event_result, "_insert_result_marker", AsyncMock(return_value=("marker", "now")))
    monkeypatch.setattr(cc_event_result, "_announce_result_marker", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cc_event_result, "_record_plan_completion", AsyncMock())
    monkeypatch.setattr(cc_event_result, "_schedule_sparse_usage_control_read", lambda _ctx: None)
    await cc_event_result.on_result(CCEventContext(live, _SID, lambda *_args: None, None), {
        "_corsia_turn_identity": identity, "is_error": False, "result": "done",
    })
    assert finish.await_args.kwargs["turn_id"] == "comment"


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "expected"), [("completed", True), ("error", None)])
async def test_claude_code_terminal_carries_fact_only_for_completed_turn(monkeypatch, status, expected):
    from unittest.mock import AsyncMock

    from subagents import cc_event_result
    from subagents.cc_event_context import CCEventContext
    from subagents.cc_runtime_state import _Live

    live = _Live(session=None, parent_thread_id=None, name="cc")
    sent = []
    monkeypatch.setattr(cc_event_result, "record_terminal_llm_usage", AsyncMock())
    monkeypatch.setattr(cc_event_result, "consume_and_attach_terminal_context_usage", AsyncMock())
    ctx = CCEventContext(live, _SID, lambda _thread, frame: sent.append(frame), _turn_db(status))
    await cc_event_result._finish_result_postprocessing(
        ctx, {"type": "turn_complete"}, None, turn_id="comment", quiet_if_unmeasured=False,
    )
    assert sent[0].get("comment_only_receipt") is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "expected"), [("completed", True), ("error", None)])
async def test_codex_terminal_carries_fact_only_for_completed_turn(monkeypatch, status, expected):
    from unittest.mock import AsyncMock

    from core.turn_core import TurnIdentity
    from subagents import codex_runtime_terminal
    from subagents.codex_runtime_state import _LiveCodex

    identity = TurnIdentity("comment", 1)
    live = _LiveCodex(session=None, parent_thread_id=None, name="codex", corsia_turn=identity)
    sent = []
    monkeypatch.setattr(codex_runtime_terminal, "_attach_terminal_context", AsyncMock())
    monkeypatch.setattr(codex_runtime_terminal, "_observe_terminal_context", lambda *_args, **_kwargs: None)
    terminal_type = "turn_complete" if status == "completed" else "turn_error"
    await codex_runtime_terminal.publish_claimed_terminal(
        thread_id=_SID, live=live, identity=identity, bcast=lambda _thread, frame: sent.append(frame),
        source="test", usage=None, terminal_type=terminal_type, db=_turn_db(status),
    )
    assert sent[0].get("comment_only_receipt") is expected
