"""Nonexhausted persisted recovery failures remain typed at every consumer."""
import asyncio

import pytest

from doceditor.live_lane import LaneRefused, Operation
from doceditor.live_publication_record import WriteActor
from doceditor.live_store import StoreRefused
from tests.test_document_live_file_tools import opened
from tests.test_document_live_transport import Socket
from tests.test_shared_document_ownership import ownership_scope as ownership_scope


@pytest.mark.asyncio
@pytest.mark.parametrize("attempts", [1, 2])
@pytest.mark.parametrize("restart", [False, True])
async def test_pending_failure_record_is_typed_in_lane_tool_and_ws(
        ownership_scope, tmp_path, monkeypatch, attempts, restart):
    from tools.filesystem_rw import WriteFileTool
    from ws import document_control
    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    for name in ("_subscriptions", "_tasks", "_openings", "_commits"):
        monkeypatch.setattr(document_control, name, {})
    reason = "specific promotion refusal"
    original = entry.live.store.promote
    def refuse(*args):
        raise StoreRefused(reason)
    monkeypatch.setattr(entry.live.store, "promote", refuse)
    actor = WriteActor(scope["owner"], "project-alpha")
    socket = Socket()
    user = {"id": scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]}
    try:
        assert not (await entry.lane.submit(Operation("replace", content="OWED", actor=actor))).server_saved
        entry.lane.retry.close()  # Hold recovery so the write barrier observes attempts 1/2.
        for _ in range(attempts):
            entry.live.store.record_failure("store_refused", reason)
        if restart:
            state_path = entry.live.store.path
            await manager.evict(entry.artifact_id, entry, can_evict=lambda: True)
            # An outside change leaves recovery held on restart; its earlier
            # nonexhausted failure remains the barrier's typed reason.
            entry.live.path.write_text("OUTSIDE")
            entry = await manager.open(entry.artifact_id, entry.live.path)
            entry.lane.retry.close()
            assert entry.live.store.path == state_path
            assert entry.live.store.failure("store_refused").attempts == attempts
        assert not entry.lane.retry.blocked_reason
        result = await entry.lane.submit(Operation("replace", content="CANNOT OVERTAKE", actor=actor))
        assert result.outcome == "refused" and isinstance(result.refusal, LaneRefused)
        with pytest.raises(LaneRefused, match=reason):
            result.require_landed()
        response = await WriteFileTool(workspace=scope["workspace"]).execute(
            path=str(entry.live.path), content="CANNOT OVERTAKE", **context)
        assert response.startswith("Error") and reason in response
        await document_control.handle_frame(socket, user, {"type": "doc_open", "path": str(entry.live.path),
            "project_id": "project-alpha", "thread_id": scope["owner"]})
        frame = await socket.next("doc_opened")
        await document_control.handle_frame(socket, user, {"type": "doc_flush", "artifact_id": entry.artifact_id,
            "store_id": frame["store_id"], "request_id": "barrier"})
        error = await socket.next("doc_error")
        assert error["kind"] == "refused" and error["message"] == reason
        assert entry.live.path.read_text() == ("OUTSIDE" if restart else "OWED")
        assert entry.live.store.pending() is not None
    finally:
        monkeypatch.setattr(entry.live.store, "promote", original)
        await document_control.disconnect(socket)
        await manager.close()
