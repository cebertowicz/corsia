"""Requester Stop joins real file tools and durable results through harness owners.

Only vendor process I/O and path admission are simulated. Stop, the lane,
registered file tool, result publication and terminal settlement are real.
"""
import asyncio
import logging
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_document_live_file_tools import opened
from tests.test_shared_document_ownership import ownership_scope as ownership_scope
from tests.test_stop_receipts import _active_turn, _settle_stop_work, _clear_live_turn_state as _clear_live_turn_state
from tests.test_document_live_lane import cut_at


class ProviderProcess:
    def __init__(self, stopped):
        self.stdout = asyncio.StreamReader()
        self.returncode = None
        self.stopped = stopped

    def kill(self, **kwargs):
        self.returncode = -9
        self.stdout.feed_eof()
        self.stopped.set()

    async def wait(self):
        return self.returncode


async def harness_owner(harness, identity, call, scope, monkeypatch, stopped, step):
    from infra import state
    from subagents import cc_runtime, codex_runtime
    sid = scope["owner"]
    if harness == "native":
        monkeypatch.setitem(state._active_turn_tasks, sid, call)
        return state._active_turns[sid]
    state._active_turns.pop(sid)
    if harness == "claude-code":
        from subagents.cc_runtime_state import _Live
        async def close():
            stopped.set()
            return -9
        session = SimpleNamespace(_request_terminate=stopped.set, terminate=close, close=close,
                                  mcp_config=None, returncode=-9)
        live = _Live(session=session, parent_thread_id=None, name="provider", corsia_turn=identity,
                     turn_active=True, admitted_tool_tasks={call}, tool_step_ids={"call": step},
                     tool_names={"call": "write_file"})
        call.add_done_callback(live.admitted_tool_tasks.discard)
        cc_runtime._LIVE.put(sid, live)
    else:
        from subagents.codex_app_server import CodexAppServerSession
        from subagents.codex_app_server_reader import read_app_server
        from subagents.codex_runtime_state import _LiveCodex
        from subagents.codex_tool_requests import ToolRequests
        session = CodexAppServerSession(binary="unused", task=None, corsia_thread_id=sid,
                                        corsia_turn_identity=identity)
        proc = ProviderProcess(stopped)
        session._proc = proc
        monkeypatch.setattr(session, "_signal_process", lambda **kwargs: proc.kill())
        session._tool_requests = ToolRequests(session)
        session._tool_requests.tasks.add(call)
        session._reader = asyncio.create_task(read_app_server(session, stdout_limit=100000,
                                                              log=logging.getLogger(__name__)))
        live = _LiveCodex(session=session, parent_thread_id=None, name="provider", corsia_turn=identity,
                          tool_step_ids={"call": step}, tool_names={"call": "write_file"})
        call.add_done_callback(session._tool_requests.tasks.discard)
        codex_runtime._LIVE.put(sid, live)
    return None


async def run_native_file_tool(scope, registry, event, identity, args, monkeypatch, *, second_call=False):
    from core import native_turn_runtime
    from core.agent import CORSIAAgent
    from core.step_handler import StepHandler
    from core.turns_steps import execute_tool_calls
    from infra import state
    agent = CORSIAAgent(workspace=scope["workspace"])
    agent.tools = registry
    async def broadcast(frame):
        if frame.get("type") == "tool_done":
            status = scope["db"].execute("SELECT status FROM active_turns WHERE id=?", (identity.turn_id,)).fetchone()[0]
            assert status not in {"completed", "cancelled", "error"}
    handler = StepHandler(scope["owner"], {"model": "test/model"}, broadcast, identity.turn_id)
    handler.bind_turn_identity(identity)
    async def one_batch(*, on_step, cancel_event, **kwargs):
        thread_context = state._current_thread_id.set(scope["owner"])
        turn_context = state._current_turn_identity.set(identity)
        try:
            calls = [SimpleNamespace(id="call", name="write_file", arguments=args)]
            if second_call:
                calls.append(SimpleNamespace(id="later", name="write_file", arguments={**args, "content": "LATER"}))
            response = SimpleNamespace(content="", reasoning_content=None, tool_calls=calls)
            await execute_tool_calls(agent, response, [], scope["owner"], cancel_event, on_step,
                                     [], "test/model", tool_result_budget_tokens=1000)
            await on_step("turn_cancelled" if cancel_event.is_set() else "turn_complete", {})
        finally:
            state._current_turn_identity.reset(turn_context)
            state._current_thread_id.reset(thread_context)
    monkeypatch.setattr(native_turn_runtime, "run_turn_with_steps", one_batch)
    turn = native_turn_runtime.NativeTurn(agent=agent, thread_id=scope["owner"], content="", model="test/model",
        turn_provider={}, handler=handler, cancel_event=event, identity=identity)
    return await native_turn_runtime.run_tracked_turn(turn)


async def owe_at_bound(entry, sid, release, monkeypatch):
    """Owe one row its publisher cannot drain until ``release``, with a bound of one row.

    The drain is healthy, never blocked, so the next write waits queued for it.
    """
    from doceditor import live_backpressure, live_publisher
    from doceditor.live_lane import Operation
    from doceditor.live_publication_record import WriteActor
    real = live_publisher.publish_one
    def stalled(store, row, **kwargs):
        while not release.is_set():
            time.sleep(0.01)
        real(store, row, **kwargs)
    monkeypatch.setattr(live_backpressure, "OWED_ROW_BOUND", 1)
    monkeypatch.setattr(live_publisher, "publish_one", stalled)
    owing = await entry.lane.submit(Operation("flush", actor=WriteActor(sid, "project-alpha")))
    assert owing.outcome == "landed" and entry.live.store.owed_count() == 1


def assert_next_turn_history(projection, harness):
    from history.adapters import to_native_history
    from subagents.cc_context_reconstruction import cc_snapshot_history
    from subagents.codex_context_reconstruction import codex_inject_items
    native = to_native_history(projection)
    owed = []
    replies = []
    for message in native:
        if message["role"] == "assistant":
            owed.extend(call["id"] for call in message.get("tool_calls", []))
        if message["role"] == "tool":
            replies.append(message["tool_call_id"])
    assert owed and sorted(owed) == sorted(replies)
    if harness == "claude-code":
        rendered = cc_snapshot_history(projection, cc_session_id="next-stop-turn")
        results = [block["tool_use_id"] for message in rendered for block in message["content"]
                   if isinstance(block, dict) and block.get("type") == "tool_result"]
        assert sorted(results) == sorted(owed)
    if harness == "codex":
        items = codex_inject_items(projection)
        text = "\n".join(block.get("text", "") for item in items for block in item["content"])
        for result in projection.messages:
            if result.role == "tool":
                assert result.content in text


async def prepare_stop_case(ownership_scope, tmp_path, monkeypatch, harness, queued):
    """Start the actual file call and stop at the selected admission boundary."""
    from host_access.processes import PROCESS_OWNER
    from infra import state
    from subagents.child_thread import persist_assistant_step
    from subagents.harness_tool_bridge import execute_tool
    from tools.filesystem_rw import ReadFileTool, WriteFileTool
    from tools.registry import ToolRegistry
    scope = ownership_scope
    from tools import host_access_gate
    async def authorize(**kwargs):
        return host_access_gate.HostActionAuthorization()
    monkeypatch.setattr(host_access_gate, "authorize_host_action", authorize)
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    sid, db = scope["owner"], scope["db"]
    identity, native_event = _active_turn(db, "live-stop", sid)
    original_broadcast = state.broadcast_to_thread
    def broadcast(thread_id, frame):
        if frame.get("type") == "tool_done":
            status = db.execute("SELECT status FROM active_turns WHERE id=?", (identity.turn_id,)).fetchone()[0]
            assert status not in {"completed", "cancelled", "error"}
        return original_broadcast(thread_id, frame)
    monkeypatch.setattr(state, "broadcast_to_thread", broadcast)
    entered, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def hold():
        entered.set()
        await release.wait()
    if queued == "drain":
        await owe_at_bound(entry, sid, release, monkeypatch)
    elif queued:
        entry.lane.schedule_setup(hold)
        await entered.wait()
    else:
        async def cut(stage):
            if stage == "record":
                await hold()
        cut_at(monkeypatch, cut)
    path = str(entry.live.path)
    await ReadFileTool(workspace=scope["workspace"]).execute(path, **context)
    args = {"path": path, "content": "REAL WRITE"}
    step = "" if harness == "native" else persist_assistant_step(sid, content="",
        tool_uses=[{"id": "call", "name": "write_file", "arguments": args}], turn_id=identity.turn_id, db=db)
    db.commit()
    registry = ToolRegistry()
    registry.register(WriteFileTool(workspace=scope["workspace"]))
    async def execute():
        if harness == "native":
            return await run_native_file_tool(scope, registry, native_event, identity, args, monkeypatch, second_call=queued)
        result = await execute_tool(registry, thread_id=sid, turn_identity=identity,
            assistant_step_id=step, provider_tool_call_id="call", invocation_id="live-stop-write",
            name="write_file", arguments=args, harness=harness)
        from subagents import cc_runtime, codex_runtime
        if harness == "claude-code":
            from subagents.cc_event_tool_settlement import settle_committed_tool_result
            await settle_committed_tool_result(live=cc_runtime._LIVE.get(sid), child_id=sid,
                identity=identity, canonical=result.delivery, bcast=state.broadcast_to_thread)
        else:
            from subagents.codex_runtime_events import settle_committed_tool_result
            await settle_committed_tool_result(live=codex_runtime._LIVE.get(sid), thread_id=sid,
                identity=identity, canonical=result.delivery, bcast=state.broadcast_to_thread)
        return result
    call = asyncio.create_task(execute())
    event = await harness_owner(harness, identity, call, scope, monkeypatch, stopped, step)
    monkeypatch.setattr(PROCESS_OWNER, "terminate_turn", AsyncMock())
    async def admitted():
        while not (entry.lane.admission.waiting if queued == "drain"
                   else entry.lane._queue if queued else entered.is_set()):
            if call.done():
                pytest.fail(f"File call ended before lane admission: {call.result()}")
            await asyncio.sleep(0)
    await asyncio.wait_for(admitted(), 3)
    return manager, entry, sid, db, identity, native_event, release, stopped, call, event


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["native", "claude-code", "codex"])
@pytest.mark.parametrize("queued", [False, True, "drain"])
async def test_requester_stop_with_actual_live_file_tool(ownership_scope, tmp_path, monkeypatch, harness, queued):
    from infra import state
    import ws
    manager, entry, sid, db, identity, native_event, release, stopped, call, event = await prepare_stop_case(
        ownership_scope, tmp_path, monkeypatch, harness, queued)
    queued_doc_state = entry.live.doc.get_update()
    queued_updates = entry.live.store.load_updates()
    queued_mirror = entry.live.store.mirror()
    user_id = db.execute("SELECT user_id FROM threads WHERE id=?", (sid,)).fetchone()[0]
    before = asyncio.get_running_loop().time()
    stop = asyncio.create_task(ws._do_stop({"id": user_id}, sid, event, None, identity.turn_id))
    try:
        if harness == "native":
            while not native_event.is_set():
                await asyncio.sleep(0)
        else:
            await asyncio.wait_for(stopped.wait(), 2)
        if harness == "native":
            if not queued:
                assert not call.done()
            release.set()
        receipt = await asyncio.wait_for(stop, 8)
        await asyncio.gather(call, return_exceptions=True)
        await _settle_stop_work()
        assert receipt.outcome == "stopped"
        assert asyncio.get_running_loop().time() - before < 10
        assert db.execute("SELECT status FROM active_turns WHERE id=?", (identity.turn_id,)).fetchone()[0] == "cancelled"
        results = db.execute("SELECT content,metadata FROM messages WHERE role='tool' AND json_extract(metadata,'$.tool_call_id')='call'").fetchall()
        assert len(results) == 1
        if queued:
            assert entry.live.path.read_text() == "a b\nsecond\n"
            metadata = json.loads(results[0]["metadata"])
            if harness == "native":
                assert "synthetic_reason" not in metadata
                payload = json.loads(results[0]["content"])
                assert payload["outcome"] == "not_started"
                assert payload["result"] == "This write did not start; nothing was written."
                assert entry.live.text == "a b\nsecond\n"
                assert entry.live.doc.get_update() == queued_doc_state
                assert entry.live.store.load_updates() == queued_updates
                assert entry.live.store.mirror() == queued_mirror
            else:
                assert metadata["synthetic_reason"] == "provider_missing_result"
            if harness == "native":
                later = db.execute("SELECT metadata FROM messages WHERE role='tool' AND json_extract(metadata,'$.tool_call_id')='later'").fetchall()
                assert len(later) == 1 and json.loads(later[0][0])["synthetic_reason"] == "user_interruption"
        else:
            if harness == "native":
                assert 'Successfully wrote' in results[0][0]
                assert 'cancelled' not in results[0][0].lower()
            else:
                from core.turns_steps import USER_INTERRUPTION_TOOL_RESULT
                prefix = "Claude Code" if harness == "claude-code" else "Codex"
                assert results[0][0] == prefix + " did not provide a result for this tool call. CORSIA will rebuild provider context before the next turn."
                assert json.loads(results[0]["metadata"])["synthetic_reason"] == "provider_missing_result"
                assert USER_INTERRUPTION_TOOL_RESULT not in results[0][0]
                terminal_rows = tuple(tuple(row) for row in db.execute("SELECT * FROM messages WHERE thread_id=? ORDER BY id", (sid,)))
                release.set()
                await entry.lane._worker
                assert tuple(tuple(row) for row in db.execute("SELECT * FROM messages WHERE thread_id=? ORDER BY id", (sid,))) == terminal_rows
            assert entry.live.text == entry.live.path.read_text() == "REAL WRITE"
        release.set()
        from history.projection import render_portable_history
        projection = render_portable_history(sid, db=db)
        replies = [message for turn in projection.turns for message in turn.messages
                   if message.role == "tool" and message.tool_call_id == "call"]
        assert len(replies) == 1
        assert_next_turn_history(projection, harness)
    finally:
        release.set()
        await asyncio.gather(call, stop, return_exceptions=True)
        state._active_turns.pop(sid, None)
        state._active_turn_tasks.pop(sid, None)
        await manager.close()
