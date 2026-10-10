"""A live open does identity setup before register-on-read can claim ownership."""
from pathlib import Path

import pytest

from doceditor.live_runtime import LiveDocuments
from registry import service, models
from tests.test_document_live_transport import Socket
from tests.test_shared_document_ownership import ownership_scope as ownership_scope, _enforce


@pytest.mark.asyncio
async def test_open_unowned_enforced_file_sets_conductor_owner(ownership_scope, tmp_path, monkeypatch):
    from ws import document_control
    from doceditor import live_runtime
    scope = ownership_scope
    path = scope["category"] / "live.md"
    path.write_text("old")
    _enforce(scope)
    manager = LiveDocuments(tmp_path / "live", workspace=scope["workspace"],
                            db_path=Path(scope["db"].execute("PRAGMA database_list").fetchone()[2]))
    monkeypatch.setattr(live_runtime, "_manager", manager)
    user = {"id": scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["other"],)).fetchone()[0]}
    socket = Socket()
    try:
        await document_control.handle_frame(socket, user, {"type": "doc_open", "path": str(path),
                                                         "project_id": "project-alpha", "thread_id": scope["other"]})
        assert (await socket.next("doc_opened"))["live"]
        artifact = service.find_file_identity(str(path), db=scope["db"])
        routing = models.routing_registrations(artifact["integration"], artifact["type"], artifact["external_id"], db=scope["db"])
        assert routing["owner"]["parent_thread_id"] == scope["conductor"]
    finally:
        await document_control.disconnect(socket)
        await manager.close()


@pytest.mark.asyncio
async def test_open_setup_failure_is_refused_then_reopen_succeeds(ownership_scope, tmp_path, monkeypatch):
    from ws import document_control
    from doceditor import live_runtime
    from registry import shared_documents
    scope = ownership_scope
    path = scope["category"] / "live.md"
    path.write_text("old")
    _enforce(scope)
    manager = LiveDocuments(tmp_path / "live", workspace=scope["workspace"],
                            db_path=Path(scope["db"].execute("PRAGMA database_list").fetchone()[2]))
    monkeypatch.setattr(live_runtime, "_manager", manager)
    real = shared_documents.resolve_shared_target
    def unavailable(*args, **kwargs):
        raise OSError("index is locked")
    monkeypatch.setattr(shared_documents, "resolve_shared_target", unavailable)
    user = {"id": scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["other"],)).fetchone()[0]}
    frame = {"type": "doc_open", "path": str(path), "project_id": "project-alpha", "thread_id": scope["other"]}
    socket = Socket()
    try:
        await document_control.handle_frame(socket, user, frame)
        refused = await socket.next("doc_error")
        assert refused["kind"] == "setup_failed" and "index is locked" in refused["message"]
        assert manager.documents == {}
        monkeypatch.setattr(shared_documents, "resolve_shared_target", real)
        await document_control.handle_frame(socket, user, frame)
        assert (await socket.next("doc_opened"))["live"]
    finally:
        await document_control.disconnect(socket)
        await manager.close()
