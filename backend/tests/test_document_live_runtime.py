"""Runtime loading shares one lane and drains durable publication without viewers."""
import asyncio
from pathlib import Path

import pytest

from doceditor.live_lane import Operation
from doceditor.live_publication_record import WriteActor
from registry import service
from registry.identity_metadata import parse_metadata
from tests.test_shared_document_ownership import ownership_scope as ownership_scope, _write, _enforce


def runtime(scope, tmp_path):
    from doceditor.live_runtime import LiveDocuments
    db_path = Path(scope["db"].execute("PRAGMA database_list").fetchone()[2])
    return LiveDocuments(tmp_path / "live", workspace=scope["workspace"], db_path=db_path)


@pytest.mark.asyncio
async def test_openers_share_store_and_leave_identity_setup_to_the_transport(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    path = scope["category"] / "live.md"
    path.write_text("old")
    _enforce(scope)
    actor = WriteActor(scope["conductor"], "project-alpha")
    manager = runtime(scope, tmp_path)
    try:
        first, second = await asyncio.gather(
            manager.open("document-id", path),
            manager.open("document-id", path))
        assert first is second
        # Open-time setup belongs to the validated load; a write sets up its own identity.
        assert service.find_file_identity(str(path), db=scope["db"]) is None
        from api.files_load import _live_open_setup
        from doceditor import live_runtime
        monkeypatch.setattr(live_runtime, "_manager", manager)
        _live_open_setup(path, "project-alpha", scope["conductor"], scope["db"])
        result = await first.lane.submit(Operation("replace", content="new", actor=actor))
        assert result.server_saved
        await first.publisher.task
        assert first.live.store.next_publication() is None
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_plain_live_write_owes_canonical_registration(ownership_scope, tmp_path):
    scope = ownership_scope
    path = scope["root"] / ".threads" / scope["owner"] / "notes.md"
    path.parent.mkdir(parents=True)
    path.write_text("old")
    artifact = service.register_write_strict(scope["owner"], str(path), db=scope["db"])
    manager = runtime(scope, tmp_path)
    try:
        actor = WriteActor(scope["owner"], "project-alpha")
        entry = await manager.open(artifact["id"], path)
        await entry.lane.submit(Operation("replace", content="new", actor=actor))
        await entry.publisher.task
        current = service.find_file_identity(str(path), db=scope["db"])
        assert parse_metadata(current["metadata"])["last_written_sha256"] == entry.live.store.mirror().file_hash
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_load_and_startup_publication_restore_latest_mirror(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    _write(scope, "live.md", "old")
    path = scope["category"] / "live.md"
    actor = WriteActor(scope["owner"], "project-alpha")
    manager = runtime(scope, tmp_path)
    entry = await manager.open("document-id", path)
    from doceditor import live_publisher
    real = live_publisher.publish_one
    def refused(*args, **kwargs):
        raise PermissionError("publication refused")
    monkeypatch.setattr(live_publisher, "publish_one", refused)
    await entry.lane.submit(Operation("replace", content="new", actor=actor))
    await entry.publisher.task
    await manager.close()
    monkeypatch.setattr(live_publisher, "publish_one", real)
    fresh = runtime(scope, tmp_path)
    try:
        await fresh.startup()
        await fresh.startup_task
        await asyncio.gather(*(publisher.task for publisher in fresh.publishers.values()))
        loaded = await fresh.open("document-id", path)
        await loaded.publisher.task
        assert loaded.live.text == "new" and loaded.live.store.next_publication() is None
        artifact = service.find_file_identity(str(path), db=scope["db"])
        assert parse_metadata(artifact["metadata"])["last_written_sha256"] == loaded.live.store.mirror().file_hash
    finally:
        await fresh.close()


@pytest.mark.asyncio
async def test_guidance_open_creates_no_store(ownership_scope, tmp_path):
    scope = ownership_scope
    path = scope["workspace"] / "skills" / "sample" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: sample\ndescription: Sample\n---\n# Sample\n")
    manager = runtime(scope, tmp_path)
    try:
        assert await manager.open("guidance-id", path) is None
        assert manager.documents == {} and manager.publishers == {}
        assert not list(manager.root.glob("*.sqlite"))
    finally:
        await manager.close()
