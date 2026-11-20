"""A deleted document's autosave refusal says what happened and how to recover."""
import pytest

from tests.test_document_live_file_tools import opened
from tests.test_document_live_legacy_save import old_client, save_old
from tests.test_shared_document_ownership import ownership_scope as ownership_scope


@pytest.mark.asyncio
async def test_deleted_document_save_explains_refusal(ownership_scope, tmp_path, monkeypatch):
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    await manager.evict(entry.artifact_id, entry, can_evict=lambda: True)
    entry.live.path.unlink()
    try:
        async with old_client(ownership_scope, monkeypatch) as client:
            response = await save_old(client, entry.live.path, "unsaved text")
        assert response.status_code == 404
        assert response.json()["detail"] == (
            "This file no longer exists, so your changes weren’t saved. They’re still here; copy them before you close."
        )
        assert not entry.live.path.exists()
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_live_flush_missing_file_uses_save_recovery_copy(monkeypatch):
    from doceditor.document_save_errors import DocumentSaveError, MISSING_DOCUMENT_MESSAGE, MISSING_SAVE_MESSAGE
    from ws import document_control

    frames = []

    async def refuse(*args):
        raise DocumentSaveError('no_such_file', MISSING_DOCUMENT_MESSAGE)

    async def capture(_socket, frame):
        frames.append(frame)

    monkeypatch.setattr(document_control, '_operate', refuse)
    monkeypatch.setattr(document_control, 'send_to_client', capture)
    await document_control.handle_frame(object(), {}, {'type': 'doc_flush', 'artifact_id': 'a'})
    assert frames[0]['message'] == MISSING_SAVE_MESSAGE
