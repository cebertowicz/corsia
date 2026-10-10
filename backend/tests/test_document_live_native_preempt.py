"""Hard task cancellation is distinct from a Native lane dropping a queued call."""
import asyncio

import pytest

from tests.test_document_live_file_tools import opened
from tests.test_document_live_requester_stop import run_native_file_tool
from tests.test_shared_document_ownership import ownership_scope as ownership_scope
from tests.test_stop_receipts import _active_turn, _clear_live_turn_state as _clear_live_turn_state


@pytest.mark.asyncio
async def test_cancelled_native_turn_task_still_propagates(ownership_scope, tmp_path, monkeypatch):
    from infra import state
    from tools import host_access_gate
    from tools.filesystem_rw import ReadFileTool, WriteFileTool
    from tools.registry import ToolRegistry
    scope = ownership_scope
    async def authorize(**kwargs):
        return host_access_gate.HostActionAuthorization()
    monkeypatch.setattr(host_access_gate, "authorize_host_action", authorize)
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    identity, event = _active_turn(scope["db"], "hard-preempt", scope["owner"])
    release, entered = asyncio.Event(), asyncio.Event()
    async def hold():
        entered.set()
        await release.wait()
    entry.lane.schedule_setup(hold)
    await entered.wait()
    path = str(entry.live.path)
    await ReadFileTool(workspace=scope["workspace"]).execute(path, **context)
    registry = ToolRegistry()
    registry.register(WriteFileTool(workspace=scope["workspace"]))
    call = asyncio.create_task(run_native_file_tool(scope, registry, event, identity,
        {"path": path, "content": "NEVER WRITTEN"}, monkeypatch, second_call=True))
    try:
        async def queued():
            while not entry.lane._queue:
                if call.done():
                    pytest.fail(f"Native call ended before queueing: {call.result()}")
                await asyncio.sleep(0)
        await asyncio.wait_for(queued(), 3)
        event.set()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert scope["db"].execute("SELECT COUNT(*) FROM messages WHERE role='tool' AND json_extract(metadata,'$.synthetic_reason')='user_interruption'").fetchone()[0] == 0
        assert entry.live.path.read_text() == "a b\nsecond\n"
    finally:
        release.set()
        await asyncio.gather(call, return_exceptions=True)
        state._active_turns.pop(scope["owner"], None)
        await manager.close()
