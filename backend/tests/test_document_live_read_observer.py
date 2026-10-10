"""Successful disk reads record merge bases only on the owning event loop."""
import asyncio
import threading

import pytest

from tests.test_document_live_file_tools import opened
from tests.test_shared_document_ownership import ownership_scope as ownership_scope


@pytest.mark.asyncio
async def test_read_observer_runs_on_loop_through_eviction_and_reopen(ownership_scope, tmp_path, monkeypatch):
    from tools import filesystem_rw
    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    loop_thread = threading.get_ident()
    real = filesystem_rw.record_read_base
    observed = []
    def observer(*args):
        assert threading.get_ident() == loop_thread
        assert asyncio.get_running_loop() is loop
        real(*args)
        observed.append(tuple(manager.documents))
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(filesystem_rw, "record_read_base", observer)
    reader = filesystem_rw.ReadFileTool(workspace=scope["workspace"])
    path, artifact_id = entry.live.path, entry.artifact_id
    try:
        assert "second" in await reader.execute(str(path), **context)
        assert scope["owner"] in entry.live.read_bases
        await manager.evict(artifact_id, entry, can_evict=lambda: True)
        assert "second" in await reader.execute(str(path), **context)
        assert not entry.live.read_bases
        reopened = await manager.open(artifact_id, path)
        assert not reopened.live.read_bases
        assert "second" in await reader.execute(str(path), **context)
        assert scope["owner"] in reopened.live.read_bases
        assert observed == [(artifact_id,), (), (artifact_id,)]
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_read_finishes_after_close_open_without_touching_retired_entry(ownership_scope, tmp_path, monkeypatch):
    from tools import filesystem_rw
    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    reader = filesystem_rw.ReadFileTool(workspace=scope["workspace"])
    original = reader._read_authorized_file
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    def held(*args):
        outcome = original(*args)
        entered.set()
        assert release.wait(3)
        return outcome
    real = filesystem_rw.record_read_base
    def observe(*args):
        assert threading.get_ident() == loop_thread
        real(*args)
    monkeypatch.setattr(reader, "_read_authorized_file", held)
    monkeypatch.setattr(filesystem_rw, "record_read_base", observe)
    call = asyncio.create_task(reader.execute(str(entry.live.path), **context))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await manager.evict(entry.artifact_id, entry, can_evict=lambda: True)
        reopened = await manager.open(entry.artifact_id, entry.live.path)
        release.set()
        assert "second" in await call
        assert not entry.live.read_bases
        assert scope["owner"] in reopened.live.read_bases
    finally:
        release.set()
        await asyncio.gather(call, return_exceptions=True)
        await manager.close()
