"""One background startup pass probes stores read-only and retains only owed publishers."""
import asyncio
from pathlib import Path
import threading

import pytest

from doceditor.live_runtime import LiveDocuments
from doceditor.live_store import LiveStore
from tests.test_document_live_publisher import setup, owe
from tests.test_shared_document_ownership import ownership_scope as ownership_scope
from tests.test_document_live_backpressure import until


def runtime(scope, root):
    return LiveDocuments(root, workspace=scope["workspace"],
                         db_path=Path(scope["db"].execute("PRAGMA database_list").fetchone()[2]))


@pytest.mark.asyncio
async def test_startup_returns_without_joining_its_one_background_probe(ownership_scope, tmp_path, monkeypatch):
    store = setup(ownership_scope, tmp_path)
    manager = runtime(ownership_scope, tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls = []
    real = LiveStore.probe_owed
    def held(path):
        calls.append(path)
        entered.set()
        assert release.wait(3)
        return real(path)
    monkeypatch.setattr(LiveStore, "probe_owed", held)
    try:
        await asyncio.wait_for(manager.startup(), .5)
        assert await asyncio.to_thread(entered.wait, 1)
        await manager.startup()  # Idempotent: one pass, even if runtime calls twice.
        assert len(calls) == 1
        assert not manager.startup_task.done()
    finally:
        release.set()
        await manager.close()
    assert manager.publishers == {}
    assert store.next_publication() is None


@pytest.mark.asyncio
async def test_no_owed_row_does_not_construct_store_or_publisher_or_write_bytes(ownership_scope, tmp_path, monkeypatch):
    store = setup(ownership_scope, tmp_path)
    before = store.path.read_bytes()
    manager = runtime(ownership_scope, tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("A read-only startup probe must not initialize a store without owed rows")
    monkeypatch.setattr(LiveStore, "__init__", forbidden)
    try:
        await manager.startup()
        await manager.startup_task
        assert manager.publishers == {}
        assert store.path.read_bytes() == before
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_startup_drains_owed_row_then_drops_unloaded_publisher(ownership_scope, tmp_path):
    scope = ownership_scope
    store = setup(scope, tmp_path)
    owe(scope, store, "old", "new")
    manager = runtime(scope, tmp_path)
    try:
        await manager.startup()
        await manager.startup_task
        await until(lambda: not manager.publishers)
        assert store.next_publication() is None
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_live_publisher_is_retained_until_its_last_entry_is_evicted(ownership_scope, tmp_path, monkeypatch):
    from tests.test_document_live_file_tools import opened
    manager, entry, context = await opened(ownership_scope, tmp_path, monkeypatch)
    try:
        await entry.publisher.task
        assert manager.publishers[entry.live.store.path] is entry.publisher
        await manager.evict(entry.artifact_id, entry, can_evict=lambda: True)
        assert manager.publishers == {}
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_startup_refuses_live_page_damage_without_draining_an_older_hash(ownership_scope, tmp_path, monkeypatch):
    from tests.test_document_live_damaged_fallback import _corrupt_page
    from registry import service
    from registry.identity_metadata import parse_metadata
    from doceditor.live_document import file_hash
    scope = ownership_scope
    store = setup(scope, tmp_path)
    owe(scope, store, "old", "older")
    path = scope["category"] / "live.md"
    # This is the registry/file state after the plain damage save, with an older row still owed.
    from registry.durable_writes import write_text
    # Use the existing non-live writer and registry owner before damaging the private store.
    write_text(path, "DAMAGE SAVED", workspace=scope["workspace"], thread_id=scope["owner"],
               project_id="project-alpha")
    damaged = _corrupt_page("live")(store.path)
    manager = runtime(scope, tmp_path)
    try:
        await manager.startup()
        await manager.startup_task
        assert manager.publishers == {}
        assert store.next_publication() is not None
        artifact = service.find_file_identity(str(path), db=scope["db"])
        assert parse_metadata(artifact["metadata"])["last_written_sha256"] == file_hash(b"DAMAGE SAVED")
        assert store.path.read_bytes() == damaged
    finally:
        await manager.close()
