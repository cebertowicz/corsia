"""Real CC/Codex Stop owners settle unknown side effects before late document landing."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from tests.test_document_live_file_tools import opened
from tests.test_document_live_requester_stop import harness_owner
from tests.test_shared_document_ownership import ownership_scope as ownership_scope
from tests.test_stop_receipts import _active_turn, _settle_stop_work, _clear_live_turn_state as _clear_live_turn_state
from tests.test_document_live_lane import cut_at


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["claude-code", "codex"])
@pytest.mark.parametrize("tool_name", ["write", "edit", "multi_edit"])
async def test_stop_records_unknown_side_effects_then_late_land_and_rerun_preserves_messages(ownership_scope, tmp_path, monkeypatch, harness, tool_name):
    import ws
    from core.turns_steps import USER_INTERRUPTION_TOOL_RESULT
    from host_access.processes import PROCESS_OWNER
    from infra import state
    from subagents.child_thread import persist_assistant_step
    from subagents.harness_tool_bridge import execute_tool
    from tools import host_access_gate
    from tools.filesystem_rw import ReadFileTool, WriteFileTool
    from tools.filesystem_edit import EditFileTool, MultiEditTool
    from tools.registry import ToolRegistry
    async def authorize(**kwargs):
        return host_access_gate.HostActionAuthorization()
    monkeypatch.setattr(host_access_gate, "authorize_host_action", authorize)
    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    sid, db = scope["owner"], scope["db"]
    identity, _ = _active_turn(db, "harness-rerun", sid)
    entered, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def cut(stage):
        if stage == "record":
            entered.set()
            await release.wait()
    cut_at(monkeypatch, cut)
    path, expected = str(entry.live.path), "a B\nsecond\n"
    await ReadFileTool(workspace=scope["workspace"]).execute(path, **context)
    if tool_name == "write":
        tool, name = WriteFileTool(workspace=scope["workspace"]), "write_file"
        args = {"path": path, "content": expected}
    elif tool_name == "edit":
        tool, name = EditFileTool(workspace=scope["workspace"]), "edit_file"
        args = {"path": path, "old_string": "a b", "new_string": "a B"}
    else:
        tool, name = MultiEditTool(workspace=scope["workspace"]), "multi_edit"
        args = {"path": path, "edits": [{"old_string": "a b", "new_string": "a B"}]}
    step = persist_assistant_step(sid, content="", tool_uses=[{"id": "call", "name": name, "arguments": args}], turn_id=identity.turn_id, db=db)
    db.commit()
    registry = ToolRegistry()
    registry.register(tool)
    call = asyncio.create_task(execute_tool(registry, thread_id=sid, turn_identity=identity,
        assistant_step_id=step, provider_tool_call_id="call", invocation_id="rerun-stop",
        name=name, arguments=args, harness=harness))
    await harness_owner(harness, identity, call, scope, monkeypatch, stopped, step)
    from subagents import cc_runtime, codex_runtime
    owner = cc_runtime._LIVE.get(sid) if harness == "claude-code" else codex_runtime._LIVE.get(sid)
    owner.tool_names["call"] = name
    monkeypatch.setattr(PROCESS_OWNER, "terminate_turn", AsyncMock())
    stop = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        user_id = db.execute("SELECT user_id FROM threads WHERE id=?", (sid,)).fetchone()[0]
        stop = asyncio.create_task(ws._do_stop({"id": user_id}, sid, None, None, identity.turn_id))
        done, _ = await asyncio.wait({stop}, timeout=.5)
        assert stop in done, "real harness Stop must detach the independent write immediately"
        assert stop.result().outcome == "stopped"
        await _settle_stop_work()
        assert db.execute("SELECT status FROM active_turns WHERE id=?", (identity.turn_id,)).fetchone()[0] == "cancelled"
        rows = db.execute("SELECT content,metadata FROM messages WHERE role='tool' AND json_extract(metadata,'$.tool_call_id')='call'").fetchall()
        assert len(rows) == 1
        prefix = "Claude Code" if harness == "claude-code" else "Codex"
        expected_fallback = prefix + " did not provide a result for this tool call. CORSIA will rebuild provider context before the next turn."
        assert rows[0]["content"] == expected_fallback
        assert USER_INTERRUPTION_TOOL_RESULT not in rows[0]["content"]
        assert json.loads(rows[0]["metadata"])["synthetic_reason"] == "provider_missing_result"
        terminal_messages = tuple(tuple(row) for row in db.execute("SELECT * FROM messages WHERE thread_id=? ORDER BY id", (sid,)).fetchall())
        assert entry.live.path.read_text() == "a b\nsecond\n"
        release.set()
        await entry.lane._worker
        assert entry.live.text == entry.live.path.read_text() == expected
        assert tuple(tuple(row) for row in db.execute("SELECT * FROM messages WHERE thread_id=? ORDER BY id", (sid,)).fetchall()) == terminal_messages
        receipt = await tool.execute(**args, **context)
        assert entry.live.text == entry.live.path.read_text() == expected
        assert "No changes needed" in receipt or receipt.startswith("Error")
        assert tuple(tuple(row) for row in db.execute("SELECT * FROM messages WHERE thread_id=? ORDER BY id", (sid,)).fetchall()) == terminal_messages
    finally:
        release.set()
        await asyncio.gather(call, *([stop] if stop else []), return_exceptions=True)
        state._active_turns.pop(sid, None)
        state._active_turn_tasks.pop(sid, None)
        await manager.close()
