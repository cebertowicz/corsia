"""Real owner clears remain identifiable after an older blocking snapshot resumes."""
import asyncio

import pytest

from tests.test_document_live_file_tools import opened
from tests.test_document_live_transport import Socket
from tests.test_shared_document_ownership import ownership_scope as ownership_scope
from ws import document_control


@pytest.mark.asyncio
async def test_publisher_clear_precedes_stale_saved_snapshot(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    for registry in ("_subscriptions", "_tasks", "_openings", "_commits"):
        monkeypatch.setattr(document_control, registry, {})
    user = {"id": scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]}
    socket = Socket()
    captured, release = asyncio.Event(), asyncio.Event()
    operation = None
    original = document_control._blocking

    async def paused_snapshot(owner):
        # The flush's own landing starts a publication drain that ends in
        # budget.clear(). Let it finish first, or that clear can land between
        # the three records below and reset them (no doc_error) or erase the
        # failure before the snapshot is read (empty blocking).
        while owner.publisher.task is not None and not owner.publisher.task.done():
            await asyncio.shield(owner.publisher.task)
        for _ in range(3):
            await owner.publisher.budget.record(PermissionError("publication fixture refusal"))
        snapshot = await original(owner)
        captured.set()
        await release.wait()
        return snapshot

    try:
        await document_control.handle_frame(socket, user, {"type": "doc_open", "path": str(entry.live.path),
            "project_id": "project-alpha", "thread_id": scope["owner"], "request_id": "open"})
        admission = await socket.next("doc_opened")
        monkeypatch.setattr(document_control, "_blocking", paused_snapshot)
        operation = asyncio.create_task(document_control.handle_frame(socket, user, {
            "type": "doc_flush", "artifact_id": entry.artifact_id, "store_id": admission["store_id"],
            "request_id": "save"}))
        await asyncio.wait_for(captured.wait(), 2)
        error = await socket.next("doc_error")
        await entry.publisher.retry_blocked()
        cleared = await socket.next("doc_cleared")
        release.set()
        await operation
        saved = await socket.next("doc_saved")
        assert error["failure_generation"] <= cleared["clears_through"]
        assert saved["blocking"][0]["failure_generation"] == error["failure_generation"]
        assert cleared["kind"] == saved["blocking"][0]["kind"] == "publication_failed"
        assert entry.live.store.failure("publication_failed") is None
        import json
        print("owner_wire_frames=" + json.dumps([error, cleared, saved]))
    finally:
        release.set()
        if operation:
            await operation
        monkeypatch.setattr(document_control, "_blocking", original)
        await document_control.disconnect(socket)
        await manager.close()
