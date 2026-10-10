"""Stopped publication cannot admit writes or outlive its joined document."""
import asyncio

import pytest

from doceditor.document_save import DocumentSaveError
from doceditor.live_admission import OpenRefused
from doceditor.live_lane import Operation
from doceditor.live_publication_record import WriteActor
from doceditor.live_store import StoreRefused
from tests.test_document_live_damaged_fallback import _corrupt_page
from tests.test_document_live_file_tools import opened
from tests.test_shared_document_ownership import ownership_scope as ownership_scope


async def owe_without_drain(entry, actor):
    if entry.publisher.task:
        await entry.publisher.task
    # Mark publication requested without starting a drain, as a blocked owner is.
    def requested_without_drain():
        entry.publisher.requested = True
        entry.publisher.drained = False
    entry.lane.published = requested_without_drain
    assert (await entry.lane.submit(Operation("replace", content="LANDED", actor=actor))).server_saved
    assert entry.live.store.owed_count() == 1
    assert entry.live.text == entry.live.on_disk[1] == "LANDED"


@pytest.mark.asyncio
async def test_real_page_damage_no_change_save_refuses_and_retires_stopped_publisher(
        ownership_scope, tmp_path, monkeypatch, caplog):
    from infra import product_events
    events = []
    def emit(category, action, *, properties=None, **kwargs):
        if action == "store_damaged":
            events.append((category, properties["phase"]))
    monkeypatch.setattr(product_events, "emit_product_event", emit)
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    actor = WriteActor(ownership_scope["owner"], "project-alpha")
    try:
        await owe_without_drain(entry, actor)
        damaged = _corrupt_page("live")(entry.live.store.path)
        with pytest.raises(StoreRefused):
            entry.live.store.load_updates()
        await asyncio.sleep(0)
        await entry.lane._worker
        assert entry.publisher.closed and not entry.publisher.drained
        assert entry.live.path.read_text() == "LANDED"
        # Even a small, readable owed queue must refuse promptly after damage.
        with pytest.raises(StoreRefused, match="database disk image is malformed"):
            await asyncio.wait_for(entry.lane.submit(Operation("flush", actor=actor)), .5)
        with pytest.raises(StoreRefused):
            await entry.publisher.retry_blocked()
        with pytest.raises(OpenRefused) as refused:
            await manager.open(entry.artifact_id, entry.live.path)
        assert refused.value.kind == "store_damaged"
        assert entry.live.store.path.read_bytes() == damaged
        await manager.evict(entry.artifact_id, entry, can_evict=lambda: True)
        assert entry.live.store.path not in manager.publishers
        assert entry.live.store.owed_count() == 1
        with pytest.raises(OpenRefused) as refused:
            await manager.open(entry.artifact_id, entry.live.path)
        assert refused.value.kind == "store_damaged"
        assert events == [("documents", "open"), ("documents", "open")]
        errors = [record for record in caplog.records if "Document store refused at open" in record.getMessage()]
        assert len(errors) == 2 and all(record.levelname == "ERROR" for record in errors)
        assert entry.live.store.path.read_bytes() == damaged
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_joined_stopped_publisher_reopens_valid_store_and_drains_retained_debt(
        ownership_scope, tmp_path, monkeypatch):
    from registry import service
    from registry.identity_metadata import parse_metadata
    from doceditor.live_document import file_hash
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    actor = WriteActor(ownership_scope["owner"], "project-alpha")
    try:
        await owe_without_drain(entry, actor)
        # A classified refusal stops the owner; this fixture's store stays valid.
        entry.live.store.observe_refusal(StoreRefused("database disk image is malformed"))
        await asyncio.sleep(0)
        await entry.lane._worker
        original = entry.publisher
        assert original.closed and entry.live.store.owed_count() == 1
        await manager.evict(entry.artifact_id, entry, can_evict=lambda: True)
        assert original.task is None or original.task.done()
        assert entry.live.store.path not in manager.publishers
        reopened = await manager.open(entry.artifact_id, entry.live.path)
        assert reopened.publisher is not original and not reopened.publisher.closed
        await reopened.publisher.task
        assert reopened.live.store.owed_count() == 0
        artifact = service.find_file_identity(str(reopened.live.path), db=ownership_scope["db"])
        assert parse_metadata(artifact["metadata"])["last_written_sha256"] == file_hash(b"LANDED")
        assert reopened.live.path.read_text() == reopened.live.text == "LANDED"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_stop_while_count_is_off_loop_cannot_admit_a_write(ownership_scope, tmp_path, monkeypatch):
    import threading
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    def held_count():
        entered.set()
        assert release.wait(3)
        return 0
    monkeypatch.setattr(entry.publisher.store, "owed_count", held_count)
    left = asyncio.get_running_loop().create_future()
    try:
        call = asyncio.create_task(entry.lane.admission.admit(left, None))
        assert await asyncio.to_thread(entered.wait, 2)
        entry.publisher.stop()
        release.set()
        with pytest.raises(DocumentSaveError) as refused:
            await call
        assert refused.value.kind == "publication_failed"
        assert str(refused.value) == "Live updates couldn't be shared for this document."
    finally:
        release.set()
        await manager.close()


@pytest.mark.asyncio
async def test_damage_first_found_by_eviction_compaction_retires_publisher(ownership_scope, tmp_path, monkeypatch):
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    actor = WriteActor(ownership_scope["owner"], "project-alpha")
    try:
        await owe_without_drain(entry, actor)
        damaged = _corrupt_page("live")(entry.live.store.path)
        assert not entry.publisher.closed
        await manager.evict(entry.artifact_id, entry, can_evict=lambda: True)
        assert entry.publisher.closed
        assert entry.publisher.task is None or entry.publisher.task.done()
        assert entry.live.store.path not in manager.publishers
        assert entry.live.store.owed_count() == 1
        assert entry.live.store.path.read_bytes() == damaged
    finally:
        await manager.close()
