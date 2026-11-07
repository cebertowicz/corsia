"""Settled observation is metadata-only for a file whose lane already applied it."""
from pathlib import Path
import json

from doceditor.live_tools import write_live

import pytest

from doceditor.presence import observe_document_activity
from tests.test_document_live_file_tools import opened
from tests.test_shared_document_ownership import ownership_scope as ownership_scope


@pytest.mark.asyncio
async def test_settled_live_edit_never_reads_the_file(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    original = Path.read_bytes
    reads = []
    def read(path):
        if path == entry.live.path:
            reads.append(path)
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", read)
    try:
        activity = observe_document_activity(scope["owner"], "edit_file", {"path": str(entry.live.path),
            "new_string": "b"}, "Successfully edited", False, scope["db"])
        assert activity is not None
        assert not reads
        assert activity.frame.get("span") is None
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_settled_live_receipt_survives_eviction_without_disk_observation(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    path = entry.live.path
    receipt = await write_live(path, context, producer="edit_file", edits=())
    assert json.loads(receipt)["live_document"]
    await manager.evict(entry.artifact_id, entry, can_evict=lambda: True)
    original = Path.read_bytes
    reads = []
    def read(value):
        if value == path:
            reads.append(value)
        return original(value)
    monkeypatch.setattr(Path, "read_bytes", read)
    try:
        activity = observe_document_activity(scope["owner"], "edit_file", {"path": str(path),
            "new_string": "b"}, receipt, False, scope["db"])
        assert activity is not None
        assert not reads
        assert activity.frame["span"] is None
        assert activity.frame["revision"] == json.loads(receipt)["digest"]
    finally:
        await manager.close()
