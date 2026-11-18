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
            "This file no longer exists, so the document wasn't saved. Restore the file and try again."
        )
        assert not entry.live.path.exists()
    finally:
        await manager.close()
