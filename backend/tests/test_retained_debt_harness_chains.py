"""A refused retained result cannot wedge a Claude Code or Codex terminal.

Both harnesses pay retained results before their terminal claim, then close
whatever is still owed, then claim: ``cc_process_exit._settle_process_exit``
and ``codex_runtime._end_owned_turn``. Each test drives that real chain with a
retained unit the canonical writer refuses, and asserts the Turn still ends
with one synthetic close, two named debt lines and none of the result bytes.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from loguru import logger

from codex_runtime_fixtures import (
    _FenceSession,
    _active_live_with_persisted_tool,
    _reset_runtime,
)
from core._tool_result_delivery_support import ToolResultBindingError
from test_cc_tool_failure_fence import _live_with_result_step
from test_cc_tool_result_echo_binding import _seed_thread
from tools.recorded_call import CompletedToolCall, CompletedToolFailure

REAL = "REAL-RESULT-BYTES-OF-THE-REFUSED-UNIT"


@pytest.fixture
def errors():
    seen: list[str] = []
    sink = logger.add(lambda message: seen.append(str(message)), level="ERROR")
    yield seen
    logger.remove(sink)


@pytest.fixture
def refuse_real_results(monkeypatch):
    """Refuse every real result at the canonical writer; synthetic closes pass."""
    from core import tool_result_delivery

    original = tool_result_delivery._persist_resolved_result

    def refuse(**kwargs):
        if not (kwargs.get("extra_metadata") or {}).get("synthetic_tool_result"):
            raise ToolResultBindingError("real result refused for the chain test")
        return original(**kwargs)

    monkeypatch.setattr(tool_result_delivery, "_persist_resolved_result", refuse)


def _tool_rows(db, thread_id):
    return [(row["content"], json.loads(row["metadata"])) for row in db.execute(
        "SELECT content,metadata FROM messages WHERE thread_id=? AND role='tool'",
        (thread_id,),
    )]


def _debt(errors):
    return [line for line in errors if "stays unpaid debt" in line]


@pytest.mark.asyncio
async def test_cc_process_exit_ends_the_turn_when_its_retained_result_is_refused(
    isolated_db, monkeypatch, errors, refuse_real_results,
):
    from api import cc_attach
    from infra.database import get_db
    from subagents import cc_process_exit
    from subagents.cc_runtime_state import _LIVE

    db = get_db()
    identity = _seed_thread(db)
    frames, calls, settled = [], [], []
    broadcast = lambda _thread, frame: frames.append(frame)
    monkeypatch.setattr("infra.state.broadcast_to_thread", broadcast)
    live = await _live_with_result_step(db, identity, broadcast, calls)
    session = live.session
    original_settle = cc_process_exit._settle_process_exit

    async def watched_settle(*args, **kwargs):
        outcome = await original_settle(*args, **kwargs)
        settled.append(outcome[0])
        return outcome

    monkeypatch.setattr(cc_process_exit, "_settle_process_exit", watched_settle)

    async def failed(*_args, **_kwargs):
        raise RuntimeError("result delivery unavailable")

    monkeypatch.setattr("subagents.harness_tool_bridge._persist_delivery_off_loop", failed)

    class Registry:
        def get(self, _name):
            return None

        async def execute(self, name, arguments):
            calls.append((name, arguments))
            return REAL

    live.tool_registry = Registry()
    try:
        with pytest.raises(CompletedToolFailure) as raised:
            await cc_attach._execute_admitted_tool(live, "thread-1", {
                "name": "read_file", "arguments": {"path": "file.txt"},
                "invocation_id": "invocation", "provider_tool_call_id": "call",
            })
        [completed] = raised.value.completed
        assert completed.result == REAL and completed.canonical is None
        await asyncio.wait_for(asyncio.shield(session._terminal_driver), 2)

        assert len(settled) == 1, "the process-exit settlement chain did not run"
        turn = db.execute(
            "SELECT status,error FROM active_turns WHERE id=?", (identity.turn_id,),
        ).fetchone()
        assert turn["status"] == "error" and REAL not in turn["error"]
        [(content, metadata)] = _tool_rows(db, "thread-1")
        assert metadata["synthetic_tool_result"] is True and REAL not in content
        assert metadata["tool_call_id"] == "call"
        assert len(_debt(errors)) == 2
        assert all(REAL not in line for line in errors)
        assert len([frame for frame in frames if frame["type"] == "turn_complete"]) == 1
        assert len(calls) == 1
    finally:
        await session.terminate()
        _LIVE.pop("thread-1", None)
        db.close()


@pytest.mark.asyncio
async def test_codex_terminal_ends_the_turn_when_its_retained_result_is_refused(
    isolated_db, monkeypatch, errors, refuse_real_results,
):
    from infra.database import get_db
    from subagents import codex_runtime

    _reset_runtime()
    thread_id = "codex-retained-debt"
    session = _FenceSession(thread_id, request_idle=True)
    live, step_id = _active_live_with_persisted_tool(thread_id, session)
    identity = live.corsia_turn
    failure = CompletedToolFailure((CompletedToolCall(
        name="write_file", result=REAL, arguments={"path": "one"}, thread_id=thread_id,
        turn_id=identity.turn_id, invocation_id="call-1", provider_tool_call_id="call-1",
        assistant_step_id=step_id,
    ),))
    frames: list[dict] = []
    try:
        await codex_runtime._end_owned_turn(
            thread_id, live, identity, "error",
            bcast=lambda _thread, frame: frames.append(frame), error=failure,
        )
    finally:
        _reset_runtime()

    db = get_db()
    try:
        turn = db.execute(
            "SELECT status,error FROM active_turns WHERE id=?", (identity.turn_id,),
        ).fetchone()
        assert turn["status"] == "error" and REAL not in turn["error"]
        [(content, metadata)] = _tool_rows(db, thread_id)
        assert metadata["synthetic_tool_result"] is True and REAL not in content
        assert metadata["tool_call_id"] == "call-1"
        assert metadata["assistant_step_id"] == step_id
    finally:
        db.close()
    assert len(_debt(errors)) == 2
    assert all(REAL not in line for line in errors)
    assert not live.tool_step_ids
