"""Native Stop keeps truthful receipts while its started write settles independently."""
import asyncio
import json
import math
from unittest.mock import AsyncMock

import pytest

from tests.test_document_live_file_tools import opened
from tests.test_document_live_requester_stop import run_native_file_tool
from tests.test_shared_document_ownership import ownership_scope as ownership_scope
from tests.test_stop_receipts import _active_turn, _settle_stop_work, _clear_live_turn_state as _clear_live_turn_state
from tests.test_document_live_lane import cut_at

_TIMINGS: list[float] = []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["lands", "fails"])
@pytest.mark.parametrize("sample", [0, 1])
async def test_native_stalled_write_persists_pending_receipt_then_resolves(ownership_scope, tmp_path, monkeypatch, outcome, sample):
    from doceditor import live_lane
    from host_access.processes import PROCESS_OWNER
    from infra import state
    from tools import host_access_gate
    from tools.filesystem_rw import ReadFileTool, WriteFileTool
    from tools.registry import ToolRegistry
    import ws

    scope = ownership_scope
    async def authorize(**kwargs):
        return host_access_gate.HostActionAuthorization()
    monkeypatch.setattr(host_access_gate, "authorize_host_action", authorize)
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    sid, db = scope["owner"], scope["db"]
    identity, event = _active_turn(db, "native-stall", sid)
    monkeypatch.setattr(PROCESS_OWNER, "terminate_turn", AsyncMock())
    entered, release = asyncio.Event(), asyncio.Event()
    reason = "late write refused: volume is read-only"
    writer = entry.lane.write
    async def stalled(text, prior_hash):
        entered.set()
        await release.wait()
        if outcome == "fails":
            raise OSError(reason)
        await writer(text, prior_hash)
    monkeypatch.setattr(entry.lane, "write", stalled)
    path = str(entry.live.path)
    await ReadFileTool(workspace=scope["workspace"]).execute(path, **context)
    registry = ToolRegistry()
    registry.register(WriteFileTool(workspace=scope["workspace"]))
    call = asyncio.create_task(run_native_file_tool(scope, registry, event, identity,
        {"path": path, "content": "LATE NATIVE WRITE"}, monkeypatch))
    stop = None
    failures = []
    entry.live.failure_changed = lambda kind, message, provenance: failures.append((kind, message))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        user_id = db.execute("SELECT user_id FROM threads WHERE id=?", (sid,)).fetchone()[0]
        loop = asyncio.get_running_loop()
        before = loop.time()
        stop = asyncio.create_task(ws._do_stop({"id": user_id}, sid, event, None, identity.turn_id))
        # The production budget is used, including actual event plumbing and persisted receipts.
        receipt = await asyncio.wait_for(asyncio.shield(stop), 8)
        await call
        elapsed = loop.time() - before
        _TIMINGS.append(elapsed)
        assert elapsed < 8  # At least two seconds remain in native's 10s join.
        assert elapsed >= live_lane.NATIVE_SETTLE_TIMEOUT_S
        assert receipt.outcome == "stopped"
        [row] = db.execute("SELECT content,metadata FROM messages WHERE role='tool'").fetchall()
        assert "Its outcome is pending; read the document before writing again" in row["content"]
        assert "run it again" not in row["content"]
        assert not json.loads(row["metadata"]).get("synthetic_tool_result")
        assert not release.is_set() and not entry.lane._worker.done()
        release.set()
        await asyncio.shield(entry.lane._worker)
        if outcome == "lands":
            assert entry.live.text == entry.live.path.read_text() == "LATE NATIVE WRITE"
            assert entry.live.store.pending() is None
        else:
            assert entry.live.path.read_text() == "a b\nsecond\n"
            # A detached failure spends the normal budget; one failure does not block.
            from doceditor.live_failure_record import FailureRecord
            assert entry.live.store.failure("store_refused") == FailureRecord(1, reason, 1)
            assert not entry.lane.retry.blocked_reason and failures == []
        ordered = sorted(_TIMINGS)
        p99 = ordered[math.ceil(len(ordered) * .99) - 1]
        print(f"native_stop_receipt_samples={len(ordered)} p99_s={p99:.6f} worst_s={max(ordered):.6f} join_margin_s={10-max(ordered):.6f}")
    finally:
        release.set()
        await asyncio.gather(call, *([stop] if stop else []), return_exceptions=True)
        await _settle_stop_work()
        state._active_turns.pop(sid, None)
        state._active_turn_tasks.pop(sid, None)
        await manager.close()


@pytest.mark.asyncio
async def test_hard_cancel_during_native_event_drain_keeps_original_cancel(tmp_path, monkeypatch):
    from doceditor.live_lane import Operation
    from tests.test_document_live_lane import open_lane
    entered, release, draining = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def cut(stage):
        if stage == "record":
            entered.set()
            await release.wait()
    cut_at(monkeypatch, cut)
    live, lane = await open_lane(tmp_path)
    settle = lane._settle_native
    async def mark_drain(*args):
        draining.set()
        return await settle(*args)
    monkeypatch.setattr(lane, "_settle_native", mark_drain)
    event = asyncio.Event()
    call = asyncio.create_task(lane.submit(Operation("replace", content="RECORDED"), cancel_event=event))
    try:
        await entered.wait()
        event.set()
        await asyncio.wait_for(draining.wait(), .3)
        call.cancel("hard preemption")
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await call
        assert caught.value.args == ("hard preemption",)
        await lane._worker
        assert live.path.read_text() == "RECORDED"
    finally:
        release.set()
        await asyncio.gather(call, return_exceptions=True)
        await lane.close()
        live.close()


@pytest.mark.asyncio
async def test_native_file_adapter_never_turns_hard_cancel_into_pending_receipt(ownership_scope, tmp_path, monkeypatch):
    from infra import state
    from tools.filesystem_rw import ReadFileTool, WriteFileTool
    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    identity, event = _active_turn(scope["db"], "native-hard", scope["owner"])
    context["_host_access_turn_identity"] = identity
    entered, release, draining = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def cut(stage):
        if stage == "record":
            entered.set()
            await release.wait()
    cut_at(monkeypatch, cut)
    settle = entry.lane._settle_native
    async def mark_drain(*args):
        draining.set()
        return await settle(*args)
    monkeypatch.setattr(entry.lane, "_settle_native", mark_drain)
    path = str(entry.live.path)
    await ReadFileTool(workspace=scope["workspace"]).execute(path, **context)
    call = asyncio.create_task(WriteFileTool(workspace=scope["workspace"]).execute(
        path, content="HARD-CANCELLED WRITE", **context))
    try:
        await entered.wait()
        event.set()
        await asyncio.wait_for(draining.wait(), 1)
        call.cancel("hard preemption")
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await call
        assert type(caught.value) is asyncio.CancelledError
        assert caught.value.args == ("hard preemption",)
        await entry.lane._worker
        assert entry.live.path.read_text() == "HARD-CANCELLED WRITE"
    finally:
        release.set()
        await asyncio.gather(call, return_exceptions=True)
        state._active_turns.pop(scope["owner"], None)
        await manager.close()


@pytest.mark.asyncio
async def test_native_queued_stop_returns_not_started_receipt(ownership_scope, tmp_path, monkeypatch):
    from host_access.processes import PROCESS_OWNER
    from infra import state
    from tools import host_access_gate
    from tools.filesystem_rw import ReadFileTool, WriteFileTool
    from tools.registry import ToolRegistry
    import ws
    scope = ownership_scope
    async def authorize(**kwargs):
        return host_access_gate.HostActionAuthorization()
    monkeypatch.setattr(host_access_gate, "authorize_host_action", authorize)
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    sid, db = scope["owner"], scope["db"]
    identity, event = _active_turn(db, "native-queued", sid)
    monkeypatch.setattr(PROCESS_OWNER, "terminate_turn", AsyncMock())
    entered, release = asyncio.Event(), asyncio.Event()
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
        {"path": path, "content": "NEVER STARTED"}, monkeypatch))
    stop = None
    doc_state = entry.live.doc.get_update()
    updates = entry.live.store.load_updates()
    mirror = entry.live.store.mirror()
    try:
        async def queued():
            while not entry.lane._queue:
                await asyncio.sleep(0)
        await asyncio.wait_for(queued(), 3)
        user_id = db.execute("SELECT user_id FROM threads WHERE id=?", (sid,)).fetchone()[0]
        stop = asyncio.create_task(ws._do_stop({"id": user_id}, sid, event, None, identity.turn_id))
        assert (await asyncio.wait_for(asyncio.shield(stop), 2)).outcome == "stopped"
        await call
        [row] = db.execute("SELECT content,metadata FROM messages WHERE role='tool'").fetchall()
        assert "synthetic_reason" not in json.loads(row["metadata"])
        result = json.loads(row["content"])
        assert result["outcome"] == "not_started"
        assert result["result"] == "This write did not start; nothing was written."
        assert entry.live.doc.get_update() == doc_state
        assert entry.live.store.load_updates() == updates
        assert entry.live.store.mirror() == mirror
        assert entry.live.path.read_text() == "a b\nsecond\n"
        assert entry.live.store.pending() is None
    finally:
        release.set()
        await asyncio.gather(call, *([stop] if stop else []), return_exceptions=True)
        await _settle_stop_work()
        state._active_turns.pop(sid, None)
        state._active_turn_tasks.pop(sid, None)
        await manager.close()
