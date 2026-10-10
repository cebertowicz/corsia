"""A store that fails to open is refused by type and never replaced (design 5.7a)."""
import hashlib
import logging
import os
import sqlite3

import pytest

from doceditor.live_admission import OpenRefused
from tests.test_document_live_file_tools import opened
from tests.test_document_live_legacy_save import old_client, save_old
from tests.test_shared_document_ownership import ownership_scope as ownership_scope

DAMAGED = {
    "code": "store_damaged",
    "title": "Live editing is unavailable for this document.",
    "message": "Its saved editing history could not be read. Text in this tab is kept. Copy it to keep working.",
}


def _store_path(manager, artifact_id):
    return manager.root / (hashlib.sha256(artifact_id.encode("utf-8")).hexdigest() + ".sqlite")


async def _closed(scope, tmp_path, monkeypatch):
    """An opened then evicted document, so its next open reads the store again."""
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    await manager.evict(entry.artifact_id, entry, can_evict=lambda: True)
    return manager, entry.artifact_id, entry.live.path, context


def _events(monkeypatch):
    from infra import product_events
    seen = []
    def emit(category, action, *, properties=None, **kwargs):
        # The evicted document's publisher may still report its own retries.
        if "phase" in (properties or {}):
            seen.append((category, action, properties))
    monkeypatch.setattr(product_events, "emit_product_event", emit)
    return seen


def _damage(store):
    store.write_bytes(b"not a database " * 64)
    return store.read_bytes()


@pytest.mark.asyncio
async def test_busy_store_refuses_open_as_unavailable(ownership_scope, tmp_path, monkeypatch, caplog):
    manager, artifact_id, path, _ = await _closed(ownership_scope, tmp_path, monkeypatch)
    events = _events(monkeypatch)
    holder = sqlite3.connect(_store_path(manager, artifact_id), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with caplog.at_level(logging.ERROR), pytest.raises(OpenRefused) as refused:
            await manager.open(artifact_id, path)
        assert refused.value.kind == "store_unavailable"
        assert [(category, action) for category, action, _ in events] == [("documents", "store_unavailable")]
        assert any(record.levelno == logging.ERROR for record in caplog.records)
    finally:
        holder.rollback()
        holder.close()
        await manager.close()


@pytest.mark.asyncio
async def test_unreadable_store_refuses_open_as_unavailable(ownership_scope, tmp_path, monkeypatch):
    manager, artifact_id, path, _ = await _closed(ownership_scope, tmp_path, monkeypatch)
    store = _store_path(manager, artifact_id)
    os.chmod(store, 0)
    try:
        with pytest.raises(OpenRefused) as refused:
            await manager.open(artifact_id, path)
        assert refused.value.kind == "store_unavailable"
    finally:
        os.chmod(store, 0o600)
        await manager.close()


@pytest.mark.asyncio
async def test_damaged_store_refuses_open_and_keeps_its_bytes(ownership_scope, tmp_path, monkeypatch, caplog):
    manager, artifact_id, path, _ = await _closed(ownership_scope, tmp_path, monkeypatch)
    events = _events(monkeypatch)
    store = _store_path(manager, artifact_id)
    before, listing = _damage(store), sorted(os.listdir(manager.root))
    try:
        with caplog.at_level(logging.ERROR), pytest.raises(OpenRefused) as refused:
            await manager.open(artifact_id, path)
        assert refused.value.detail() == DAMAGED
        assert [action for _, action, _ in events] == ["store_damaged"]
        assert any(record.levelno == logging.ERROR for record in caplog.records)
        assert store.read_bytes() == before
        assert sorted(os.listdir(manager.root)) == listing
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_unavailable_store_refuses_legacy_save_then_agent_write(ownership_scope, tmp_path, monkeypatch):
    from tools.filesystem_rw import WriteFileTool
    scope = ownership_scope
    manager, artifact_id, path, context = await _closed(scope, tmp_path, monkeypatch)
    store = _store_path(manager, artifact_id)
    before = store.read_bytes()
    holder = sqlite3.connect(store, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(OpenRefused):
            await manager.open(artifact_id, path)
        async with old_client(scope, monkeypatch) as client:
            response = await save_old(client, path, "LEGACY")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "store_unavailable"
        written = await WriteFileTool(workspace=scope["workspace"]).execute(path=str(path), content="AGENT", **context)
        assert written.startswith("Error") and "store_unavailable" in written
        assert path.read_text() == "a b\nsecond\n"
        assert store.read_bytes() == before
    finally:
        holder.rollback()
        holder.close()
        await manager.close()


@pytest.mark.asyncio
async def test_successful_open_clears_the_refusal(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, artifact_id, path, _ = await _closed(scope, tmp_path, monkeypatch)
    holder = sqlite3.connect(_store_path(manager, artifact_id), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(OpenRefused):
            await manager.open(artifact_id, path)
        holder.rollback()
        entry = await manager.open(artifact_id, path)
        await manager.evict(artifact_id, entry, can_evict=lambda: True)
        async with old_client(scope, monkeypatch) as client:
            response = await save_old(client, path, "LEGACY")
        assert response.status_code == 200
        assert path.read_text() == "LEGACY"
    finally:
        holder.close()
        await manager.close()


@pytest.mark.asyncio
async def test_startup_reports_a_refused_store_and_keeps_its_bytes(ownership_scope, tmp_path, monkeypatch, caplog):
    from doceditor.live_runtime import LiveDocuments
    manager, artifact_id, _, _ = await _closed(ownership_scope, tmp_path, monkeypatch)
    await manager.close()
    events = _events(monkeypatch)
    store = _store_path(manager, artifact_id)
    before = _damage(store)
    restarted = LiveDocuments(manager.root, workspace=manager.workspace, db_path=manager.db_path)
    with caplog.at_level(logging.ERROR):
        await restarted.startup()
        await restarted.startup_task
    await restarted.close()
    assert [action for _, action, _ in events] == ["store_damaged"]
    assert any(record.levelno == logging.ERROR for record in caplog.records)
    assert store.read_bytes() == before


@pytest.mark.asyncio
async def test_open_refusal_frame_carries_kind_and_title(monkeypatch):
    from ws import document_control
    sent = []
    async def send(socket, frame):
        sent.append(frame)
    async def refuse(socket, user, data):
        raise OpenRefused("store_damaged", DAMAGED["message"], title=DAMAGED["title"])
    monkeypatch.setattr(document_control, "send_to_client", send)
    monkeypatch.setattr(document_control, "_open", refuse)
    await document_control.handle_frame(object(), {"id": "u"}, {"type": "doc_open", "request_id": "r"})
    assert sent[0]["kind"] == "store_damaged"
    assert sent[0]["title"] == DAMAGED["title"] and sent[0]["message"] == DAMAGED["message"]
