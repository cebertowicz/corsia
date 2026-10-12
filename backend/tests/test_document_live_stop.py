"""Harness Stop owners keep Native results and detach real CC/Codex callers.

Vendor process I/O is simulated; registry execution, the lane, terminal
settlement and real provider result owners remain exercised.
"""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from doceditor.live_lane import Operation
from tests.plan_execution_helpers import ProbeTool, registry_of
from tests.test_cc_tool_result_echo_binding import _seed_thread
from tests.test_document_live_lane import open_lane, cut_at
from tests.test_stop_receipts import _clear_live_turn_state as _clear_live_turn_state


class LaneProbe(ProbeTool):
    def __init__(self, lane, cancel_event):
        super().__init__("lane_probe")
        self.lane = lane
        self.cancel_event = cancel_event

    async def execute(self, **kwargs):
        result = await self.lane.submit(Operation("replace", content="REAL WRITE"), cancel_event=self.cancel_event)
        return f"written:{result.text};stored:{result.server_saved}"


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["claude-code", "codex", "native"])
async def test_started_stop_settles_caller_and_keeps_lane_owned_late_result(tmp_path, isolated_db, monkeypatch, harness):
    import ws
    from core.turn_core import settle_turn_terminal
    from core.turns_steps import USER_INTERRUPTION_TOOL_RESULT
    from host_access.processes import PROCESS_OWNER
    from infra import state
    from infra.database import get_db
    from subagents.child_thread import persist_assistant_step
    from subagents.harness_tool_bridge import execute_tool
    from tests.test_document_live_requester_stop import harness_owner
    from tests.test_stop_receipts import _settle_stop_work
    entered, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def cut(stage):
        if stage == "record":
            entered.set()
            await release.wait()
    cut_at(monkeypatch, cut)
    live, lane = await open_lane(tmp_path)
    db = get_db()
    identity = _seed_thread(db)
    step = persist_assistant_step("thread-1", content="", tool_uses=[{"id": "call", "name": "lane_probe", "arguments": {}}], turn_id=identity.turn_id, db=db)
    db.commit()
    cancel = asyncio.Event()
    cancel.turn_identity = identity
    monkeypatch.setitem(state._active_turns, "thread-1", cancel)
    registry = registry_of(LaneProbe(lane, cancel))
    call = asyncio.create_task(execute_tool(registry, thread_id="thread-1", turn_identity=identity,
        assistant_step_id=step, provider_tool_call_id="call", invocation_id="lane-write", name="lane_probe", arguments={}, harness=harness))
    join = None
    try:
        await entered.wait()
        if harness == "native":
            from core import native_turn_control
            async def no_owned_processes(*args):
                pass
            monkeypatch.setitem(state._active_turn_tasks, "thread-1", call)
            monkeypatch.setattr(native_turn_control, "_terminate_native_turn_processes", no_owned_processes)
            join = asyncio.create_task(native_turn_control.cancel_and_join_native_turn("thread-1", expected_turn=identity))
            await asyncio.sleep(0)
            assert not call.done()
            release.set()
            await asyncio.wait_for(join, 5)
            result = await call
            assert result.startswith("written:REAL WRITE;stored:True")
            await settle_turn_terminal(identity.turn_id, "cancelled")
            [row] = db.execute("SELECT content,metadata FROM messages WHERE role='tool'").fetchall()
            assert row["content"] == "written:REAL WRITE;stored:True"
            assert json.loads(row["metadata"])["tool_result_binding_state"] == "bound"
        else:
            await harness_owner(harness, identity, call, {"owner": "thread-1", "db": db}, monkeypatch, stopped, step)
            from subagents import cc_runtime, codex_runtime
            owner = cc_runtime._LIVE.get("thread-1") if harness == "claude-code" else codex_runtime._LIVE.get("thread-1")
            owner.tool_names["call"] = "lane_probe"
            monkeypatch.setattr(PROCESS_OWNER, "terminate_turn", AsyncMock())
            join = asyncio.create_task(ws._do_stop({"id": "user-1"}, "thread-1", None, None, identity.turn_id))
            receipt = await asyncio.wait_for(join, 5)
            assert receipt.outcome == "stopped"
            assert not release.is_set()
            assert not lane._worker.done()
            await _settle_stop_work()
            with pytest.raises(asyncio.CancelledError):
                await call
            [row] = db.execute("SELECT content,metadata FROM messages WHERE role='tool'").fetchall()
            prefix = "Claude Code" if harness == "claude-code" else "Codex"
            assert row["content"] == prefix + " did not provide a result for this tool call. CORSIA will rebuild provider context before the next turn."
            assert USER_INTERRUPTION_TOOL_RESULT not in row["content"]
            assert json.loads(row["metadata"])["synthetic_reason"] == "provider_missing_result"
            terminal = tuple(tuple(row) for row in db.execute("SELECT * FROM messages ORDER BY id"))
            release.set()
            await lane._worker
            assert tuple(tuple(row) for row in db.execute("SELECT * FROM messages ORDER BY id")) == terminal
        assert live.text == live.path.read_text() == "REAL WRITE"
        assert db.execute("SELECT status FROM active_turns WHERE id=?", (identity.turn_id,)).fetchone()[0] == "cancelled"
    finally:
        release.set()
        await asyncio.gather(call, *([join] if join else []), return_exceptions=True)
        await lane.close()
        live.close()
        db.close()
