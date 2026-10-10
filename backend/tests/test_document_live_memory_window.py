"""A promoted write yields with durable memory ahead of live, then converges."""
import asyncio
import base64
import json
import sys
from unittest.mock import AsyncMock

import pytest

from doceditor.live_lane import Operation
from doceditor.live_publication_record import WriteActor
from doceditor.live_text import clone
from tests.test_document_live_file_tools import opened
from tests.test_document_live_requester_stop import harness_owner
from tests.test_shared_document_ownership import ownership_scope as ownership_scope
from tests.test_stop_receipts import _active_turn, _settle_stop_work, _clear_live_turn_state as _clear_live_turn_state


def pin_memory(entry, monkeypatch):
    from doceditor import live_document
    entered, release = asyncio.Event(), asyncio.Event()
    original = live_document.yield_loop_pass
    async def pinned():
        caller = sys._getframe(1)
        if (caller.f_code.co_name == "apply_committed"
                and caller.f_locals.get("self") is entry.live and not entered.is_set()):
            entered.set()
            await release.wait()
            return
        await original()
    monkeypatch.setattr(live_document, "yield_loop_pass", pinned)
    return entered, release, asyncio.sleep


def user(scope):
    return {"id": scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]}


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["claude-code", "codex"])
async def test_memory_checkpoint_stop_and_queued_same_author_write_converge(ownership_scope, tmp_path, monkeypatch, harness):
    import ws
    from core.turns_steps import USER_INTERRUPTION_TOOL_RESULT
    from host_access.processes import PROCESS_OWNER
    from infra import state
    from subagents.child_thread import persist_assistant_step
    from subagents.harness_tool_bridge import execute_tool
    from tools import host_access_gate
    from tools.filesystem_rw import ReadFileTool, WriteFileTool
    from tools.registry import ToolRegistry
    async def authorize(**kwargs):
        return host_access_gate.HostActionAuthorization()
    monkeypatch.setattr(host_access_gate, "authorize_host_action", authorize)
    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    entered, release, sleep = pin_memory(entry, monkeypatch)
    sid, db = scope["owner"], scope["db"]
    identity, _ = _active_turn(db, "memory-checkpoint", sid)
    path, expected = str(entry.live.path), "a B\nsecond\n"
    await ReadFileTool(workspace=scope["workspace"]).execute(path, **context)
    tool = WriteFileTool(workspace=scope["workspace"])
    args = {"path": path, "content": expected}
    step = persist_assistant_step(sid, content="", tool_uses=[{"id": "call", "name": "write_file", "arguments": args}], turn_id=identity.turn_id, db=db)
    db.commit()
    registry = ToolRegistry()
    registry.register(tool)
    call = asyncio.create_task(execute_tool(registry, thread_id=sid, turn_identity=identity,
        assistant_step_id=step, provider_tool_call_id="call", invocation_id="memory-stop",
        name="write_file", arguments=args, harness=harness))
    await harness_owner(harness, identity, call, scope, monkeypatch, asyncio.Event(), step)
    from subagents import cc_runtime, codex_runtime
    owner = cc_runtime._LIVE.get(sid) if harness == "claude-code" else codex_runtime._LIVE.get(sid)
    owner.tool_names["call"] = "write_file"
    monkeypatch.setattr(PROCESS_OWNER, "terminate_turn", AsyncMock())
    queued = stop = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert entry.live.path.read_text() == str(entry.live.durable["text"]) == expected
        assert entry.live.text == "a b\nsecond\n"
        queued = asyncio.create_task(tool.execute(**args, **context))
        await sleep(.01)
        assert not queued.done(), "the same document writer must stay behind the current lane item"
        stop = asyncio.create_task(ws._do_stop(user(scope), sid, None, None, identity.turn_id))
        assert (await asyncio.wait_for(stop, .5)).outcome == "stopped"
        await _settle_stop_work()
        rows = db.execute("SELECT content,metadata FROM messages WHERE role='tool' AND json_extract(metadata,'$.tool_call_id')='call'").fetchall()
        assert len(rows) == 1
        prefix = "Claude Code" if harness == "claude-code" else "Codex"
        assert rows[0]["content"] == prefix + " did not provide a result for this tool call. CORSIA will rebuild provider context before the next turn."
        assert USER_INTERRUPTION_TOOL_RESULT not in rows[0]["content"]
        assert json.loads(rows[0]["metadata"])["synthetic_reason"] == "provider_missing_result"
        terminal = tuple(tuple(row) for row in db.execute("SELECT * FROM messages WHERE thread_id=? ORDER BY id", (sid,)).fetchall())
        release.set()
        receipt = await asyncio.wait_for(queued, 2)
        await entry.lane._worker
        assert entry.live.text == str(entry.live.durable["text"]) == entry.live.path.read_text() == expected
        assert "No changes needed" in receipt
        assert tuple(tuple(row) for row in db.execute("SELECT * FROM messages WHERE thread_id=? ORDER BY id", (sid,)).fetchall()) == terminal
    finally:
        release.set()
        await asyncio.gather(call, *([queued] if queued else []), *([stop] if stop else []), return_exceptions=True)
        state._active_turns.pop(sid, None)
        state._active_turn_tasks.pop(sid, None)
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["doc_opened", "doc_sync"])
@pytest.mark.parametrize("local_edit", [False, True])
async def test_actual_join_routes_receive_later_delta_and_keep_unacked_local_edit(ownership_scope, tmp_path, monkeypatch, route, local_edit):
    from ws import document_control as control
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    entered, release, sleep = pin_memory(entry, monkeypatch)
    sent = {}
    def send(socket, frame):
        sent.setdefault(socket, []).append(dict(frame))
        done = asyncio.get_running_loop().create_future()
        done.set_result(None)
        return done
    monkeypatch.setattr(control, "send_to_client", send)
    monkeypatch.setattr(control, "_subscriptions", {})
    monkeypatch.setattr(control, "_commits", {})
    socket = object()
    open_data = {"path": str(entry.live.path), "project_id": "project-alpha", "thread_id": scope["owner"]}
    client = clone(entry.live.doc.get_update())
    local_update = None
    if local_edit:
        before = client.get_state()
        client["text"].insert(len(str(client["text"]).encode()), " LOCAL")
        local_update = client.get_update(before)
    if route == "doc_sync":
        await control._open(socket, user(scope), open_data)
        sent[socket].clear()
    expected = "a B\nsecond\n"
    call = asyncio.create_task(entry.lane.submit(Operation("replace", content=expected, actor=WriteActor(scope["owner"], "project-alpha"))))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert str(entry.live.durable["text"]) == expected
        assert entry.live.text == "a b\nsecond\n"
        if route == "doc_opened":
            await control._open(socket, user(scope), open_data)
        else:
            await control._operate(socket, user(scope), {"type": "doc_sync", "artifact_id": entry.artifact_id,
                "store_id": entry.live.store.store_id, "vector": base64.b64encode(client.get_state()).decode()})
        assert entry.artifact_id in control._subscriptions[socket]
        [snapshot] = [frame for frame in sent[socket] if frame["type"] == ("doc_opened" if route == "doc_opened" else "doc_update")]
        client.apply_update(base64.b64decode(snapshot["update"]))
        assert str(client["text"]) == "a b\nsecond\n" + (" LOCAL" if local_edit else "")
        sent[socket].clear()
        release.set()
        assert (await call).server_saved
        await sleep(0)
        deltas = [frame for frame in sent[socket] if frame["type"] == "doc_update"]
        assert deltas, "a registered joiner must receive the post-live-apply broadcast"
        for frame in deltas:
            client.apply_update(base64.b64decode(frame["update"]))
        assert str(client["text"]) == expected + (" LOCAL" if local_edit else "")
        if local_update:
            await control._operate(socket, user(scope), {"type": "doc_update", "artifact_id": entry.artifact_id,
                "store_id": entry.live.store.store_id, "message_id": "retained-local",
                "update": base64.b64encode(local_update).decode()})
            await asyncio.gather(*tuple(control._commits.values()))
        assert str(client["text"]) == str(entry.live.durable["text"]) == entry.live.text
    finally:
        release.set()
        await asyncio.gather(call, return_exceptions=True)
        await asyncio.gather(*tuple(control._commits.values()), return_exceptions=True)
        await manager.close()
