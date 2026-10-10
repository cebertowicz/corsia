"""Reopening imports closed-file changes through the lane before first browser sync."""
import pytest

from doceditor.document_save import DocumentSave, save_document_text
from doceditor.live_lane import Operation
from doceditor.live_publication_record import WriteActor
from doceditor.live_store import BrowserMessage, LiveStore, StoreRefused
from doceditor.live_text import clone
from registry import service
from tests.test_document_live_runtime import runtime
from tests.test_shared_document_ownership import ownership_scope as ownership_scope, _write


@pytest.mark.asyncio
async def test_reopen_imports_legacy_save_before_sync_and_merges_offline_copy(ownership_scope, tmp_path, monkeypatch):
    from doceditor import live_runtime

    scope = ownership_scope
    _write(scope, "live.md", "base")
    path = scope["category"] / "live.md"
    artifact = service.find_file_identity(str(path), db=scope["db"])
    actor = WriteActor(scope["owner"], "project-alpha")
    manager = runtime(scope, tmp_path)
    monkeypatch.setattr(live_runtime, "_manager", manager)
    user_id = scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]
    try:
        first = await manager.open(artifact["id"], path)
        old_store_id = first.live.store.store_id
        offline = clone(first.live.doc.get_update())
        vector = offline.get_state()
        offline["text"].insert(0, "OFFLINE ")
        await manager.evict(artifact["id"], first, can_evict=lambda: True)
        await save_document_text(DocumentSave(path=str(path), text="base DISK", project_id="project-alpha"), user_id)
        assert path.read_text() == "base DISK"
        loaded = await manager.open(artifact["id"], path)
        assert loaded.live.text == "base DISK"
        assert loaded.live.store.store_id == old_store_id
        assert loaded.live.store.pending() is None
        loaded.live.receive(BrowserMessage("offline", offline.get_update(vector)))
        await loaded.lane.submit(Operation("flush", actor=actor))
        assert loaded.live.text == path.read_text() == "OFFLINE base DISK"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_open_keeps_held_row_and_both_texts_without_importing(ownership_scope, tmp_path):
    from doceditor.live_document import file_hash
    from doceditor.live_store import PendingWrite
    from doceditor.live_text import apply_text

    scope = ownership_scope
    _write(scope, "live.md", "base")
    path = scope["category"] / "live.md"
    artifact = service.find_file_identity(str(path), db=scope["db"])
    manager = runtime(scope, tmp_path)
    try:
        first = await manager.open(artifact["id"], path)
        fork = clone(first.live.doc.get_update())
        apply_text(fork, "interrupted candidate")
        pending = PendingWrite("interrupted", fork.get_update(first.live.doc.get_state()), fork.get_update(),
                               file_hash(b"base"), file_hash(b"interrupted candidate"))
        first.live.store.record_pending(pending)
        path.write_text("outside")
        first.live.store.mark_held(pending.op_id, file_hash(b"outside"))
        held = first.live.store.pending()
        before = first.live.store.load_updates()
        await manager.evict(artifact["id"], first, can_evict=lambda: True)
        loaded = await manager.open(artifact["id"], path)
        assert loaded.live.text == "base" and path.read_text() == "outside"
        assert loaded.live.store.pending() == held and loaded.live.store.load_updates() == before
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_refused_open_import_keeps_disk_and_durable_store_for_retry(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    _write(scope, "live.md", "base")
    path = scope["category"] / "live.md"
    artifact = service.find_file_identity(str(path), db=scope["db"])
    manager = runtime(scope, tmp_path)
    try:
        first = await manager.open(artifact["id"], path)
        old_updates = first.live.store.load_updates()
        old_mirror = first.live.store.mirror()
        state_path = first.live.store.path
        await manager.evict(artifact["id"], first, can_evict=lambda: True)
        path.write_text("base DISK")
        original = LiveStore.commit_import
        def refused(*args, **kwargs):
            raise StoreRefused("import refused")
        monkeypatch.setattr(LiveStore, "commit_import", refused)
        with pytest.raises(StoreRefused, match="import refused"):
            await manager.open(artifact["id"], path)
        assert manager.documents == {}
        stored = LiveStore(state_path)
        assert stored.load_updates() == old_updates and stored.mirror() == old_mirror
        assert path.read_text() == "base DISK"
        monkeypatch.setattr(LiveStore, "commit_import", original)
        loaded = await manager.open(artifact["id"], path)
        assert loaded.live.text == "base DISK" == path.read_text()
    finally:
        await manager.close()
