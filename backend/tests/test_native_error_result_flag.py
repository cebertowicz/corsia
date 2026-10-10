"""A native error result is stored with the canonical error flag, and its readers see it.

The native writer stamps ``is_error`` from ``is_error_result`` like every
harness writer does. Each test stores one real native error result and then
runs one reader of that flag against the stored row.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from core import step_handler
from core.turn_core import TurnIdentity
from infra import database
from infra.queries import insert_message

THREAD, TURN, STEP, CALL = "native-thread", "native-turn", "native-step", "native-call"
FAILED = "Error: the disk refused the write"


@pytest.fixture
def native(isolated_db, monkeypatch, tmp_path):
    """One running native Turn whose Step declares one ``write_file`` call."""
    arguments = {"path": str(tmp_path / "doc.md"), "content": "body"}
    with database.write_transaction(None, scope="seed_native_error_flag_test") as db:
        db.execute(
            "INSERT INTO threads (id,user_id,title,model,created_at,updated_at) "
            f"VALUES ('{THREAD}','fixture-user','original','test/model','t','t')",
        )
        db.execute(
            "INSERT INTO active_turns "
            "(id,thread_id,execution_epoch,model,status,started_at,boot_epoch) "
            f"VALUES ('{TURN}','{THREAD}',0,'test/model','running','t','fixture-boot')",
        )
        insert_message(THREAD, "user", "write the file", {"turn_id": TURN}, db=db)
        insert_message(THREAD, "assistant", "", {"turn_id": TURN, "llm_tool_calls": [{
            "id": CALL, "type": "function",
            "function": {"name": "write_file", "arguments": json.dumps(arguments)},
        }]}, msg_id=STEP, db=db)

    async def broadcast(_frame):
        return None

    async def run_job(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(step_handler, "run_in_db", run_job)
    handler = step_handler.StepHandler(THREAD, {"model": "test/model"}, broadcast, TURN)
    handler.bind_turn_identity(TurnIdentity(TURN, 0))
    handler.tool_step_id = STEP
    handler.tool_calls = [{
        "id": CALL, "name": "write_file", "args": json.dumps(arguments), "result": None,
    }]
    return handler


def _complete(handler, result):
    return asyncio.run(handler.handle("tool_complete", {
        "id": CALL, "name": "write_file", "result": result,
    }))


def _stored():
    db = database.get_db()
    try:
        return [dict(row) for row in db.execute(
            "SELECT * FROM messages WHERE thread_id=? ORDER BY seq", (THREAD,),
        )]
    finally:
        db.close()


def _tool_metadata():
    [row] = [row for row in _stored() if row["role"] == "tool"]
    return json.loads(row["metadata"])


def test_error_result_is_stored_and_returned_with_the_flag(native):
    portable = _complete(native, FAILED)

    assert _tool_metadata()["is_error"] is True
    assert portable.is_error is True


def test_success_result_carries_no_flag(native):
    portable = _complete(native, "wrote 4 bytes")

    assert "is_error" not in _tool_metadata()
    assert portable.is_error is False


def test_reloaded_display_call_is_marked_failed(native):
    from core.message_display import llm_to_display

    _complete(native, FAILED)

    [step] = [row for row in llm_to_display(_stored()) if row.get("role") == "assistant"]
    [call] = json.loads(step["metadata"])["tool_calls"]
    assert call["is_error"] is True and call["result"] == FAILED


def test_portable_history_checkpoint_and_claude_replay_carry_the_error(native):
    from history.adapters import checkpoint_source_block
    from history.projection import render_portable_history
    from subagents.cc_transcript_projection import project_cc_messages

    _complete(native, FAILED)

    db = database.get_db()
    try:
        projection = render_portable_history(THREAD, db=db, include_unfinished_turn=True)
    finally:
        db.close()
    [tool] = [message for message in projection.messages if message.role == "tool"]
    assert tool.is_error is True
    assert " error=true" in checkpoint_source_block(tool)["content"].splitlines()[1]
    replayed = project_cc_messages(projection, cc_session_id="replay")
    [block] = [
        block for projected in replayed if projected.source.role == "tool"
        for block in projected.message["content"]
    ]
    assert block["is_error"] is True


@pytest.mark.parametrize(("result", "flag"), [(FAILED, True), ("wrote 4 bytes", False)])
def test_registration_observer_receives_the_stored_flag(native, monkeypatch, result, flag):
    from registry import observer

    seen: list[bool] = []

    def activity(_thread, _name, _args, _text, is_error, _db, *, call_id=None):
        seen.append(is_error)
        return None

    monkeypatch.setattr(observer, "observe_document_activity", activity)
    registered: list[tuple] = []
    monkeypatch.setattr(
        observer, "register_observed_write",
        lambda *args, **kwargs: registered.append((args, kwargs)),
    )
    _complete(native, result)

    assert seen == [flag]
    assert bool(registered) is (not flag)
