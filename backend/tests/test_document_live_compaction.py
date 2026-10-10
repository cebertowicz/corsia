"""Journal compaction preserves CRDT identities and never rewrites pending evidence."""
import asyncio

import pytest

from doceditor.live_document import LiveDocument, file_hash
from doceditor.live_store import BrowserMessage, PendingWrite, StoreRefused
from doceditor.live_text import clone
from tests.test_document_live_lane import insert, open_lane
from tests.test_shared_document_ownership import ownership_scope as ownership_scope


@pytest.mark.asyncio
async def test_compaction_keeps_text_store_id_and_device_copy_without_duplication(tmp_path):
    live, lane = await open_lane(tmp_path)
    peer = clone(live.doc.get_update())
    insert(live, "acked", "SERVER ")
    await live.commit_received()
    original_id = live.store.store_id
    vector = peer.get_state()
    peer["text"].insert(0, "DEVICE ")
    device_update = peer.get_update(vector)
    # Store unit seam: production compaction calls belong only to LiveDocuments.
    live.store.compact(live.durable.get_update())
    assert len(live.store.load_updates()) == 1
    live.close()
    reopened = await LiveDocument.open(live.path, live.store.path)
    assert reopened.store.store_id == original_id
    assert reopened.text == "SERVER hello quote"
    peer.apply_update(reopened.doc.get_update(peer.get_state()))
    assert reopened.receive(BrowserMessage("device", device_update))
    await reopened.commit_received()
    assert reopened.text == str(peer["text"])
    assert reopened.text.count("DEVICE ") == reopened.text.count("SERVER ") == 1
    reopened.close()
    await lane.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "held"])
async def test_compaction_refuses_pending_or_held_evidence_without_changes(tmp_path, state):
    live, lane = await open_lane(tmp_path)
    insert(live, "acked", "ACKED ")
    await live.commit_received()
    candidate = live.doc.get_update()
    live.store.record_pending(PendingWrite("op", candidate, candidate,
                                          file_hash(live.path.read_bytes()), file_hash(live.text.encode())))
    if state == "held":
        live.store.mark_held("op", file_hash(b"outside"))
    before = live.store.path.read_bytes()
    updates = live.store.load_updates()
    pending = live.store.pending()
    with pytest.raises(StoreRefused, match="pending write"):
        live.store.compact(live.durable.get_update())
    assert live.store.path.read_bytes() == before
    assert live.store.pending() == pending and live.store.load_updates() == updates
    await lane.close()
    live.close()


@pytest.mark.asyncio
async def test_runtime_compacts_only_after_promote_and_at_eviction_under_lock(ownership_scope, tmp_path, monkeypatch):
    from doceditor import live_runtime
    from doceditor.live_lane import Operation
    from doceditor.live_publication_record import WriteActor
    from tests.test_document_live_file_tools import opened
    monkeypatch.setattr(live_runtime, "LIVE_ROW_BOUND", 3)
    manager, entry, context = await opened(ownership_scope, tmp_path, monkeypatch)
    real, calls = entry.live.store.compact, []
    def checked(state):
        assert entry.live.committer_lock.locked()
        assert entry.live.store.pending() is None
        calls.append(state)
        real(state)
    monkeypatch.setattr(entry.live.store, "compact", checked)
    actor = WriteActor(ownership_scope["owner"], "project-alpha")
    try:
        for index in range(3):
            insert(entry.live, f"acked-{index}", str(index))
            await entry.live.commit_received()
        assert calls == []  # Typing alone has no timer or compaction writer.
        assert entry.live.store.live_count() == 4
        assert (await entry.lane.submit(Operation("flush", actor=actor))).server_saved
        assert len(calls) == 1 and entry.live.store.live_count() == 1
        insert(entry.live, "acked-last", "LAST ")
        await entry.live.commit_received()
        insert(entry.live, "unacked", "RECEIVED ")
        await manager.evict(entry.artifact_id, entry, can_evict=lambda: True,
                            before_close=entry.live.commit_received)
        assert len(calls) == 2
        reopened = await LiveDocument.open(entry.live.path, entry.live.store.path)
        assert len(reopened.store.load_updates()) == 1
        assert "RECEIVED LAST " in reopened.text
        reopened.close()
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_eviction_keeps_pending_row_and_journal(ownership_scope, tmp_path, monkeypatch):
    from tests.test_document_live_file_tools import opened
    manager, entry, context = await opened(ownership_scope, tmp_path, monkeypatch)
    insert(entry.live, "acked", "ACKED ")
    await entry.live.commit_received()
    state = entry.live.durable.get_update()
    entry.live.store.record_pending(PendingWrite("op", state, state, file_hash(b"prior"), file_hash(b"result")))
    entry.live.store.mark_held("op", file_hash(b"outside"))
    pending, updates = entry.live.store.pending(), entry.live.store.load_updates()
    await manager.evict(entry.artifact_id, entry, can_evict=lambda: True)
    assert entry.live.store.pending() == pending and entry.live.store.load_updates() == updates
    await manager.close()
