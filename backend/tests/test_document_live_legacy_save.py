"""A legacy HTTP save cannot bypass an already-started live file write."""
import asyncio

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from doceditor.live_lane import Operation
from doceditor.live_publication_record import WriteActor
from tests.test_document_live_file_tools import opened
from tests.test_shared_document_ownership import ownership_scope as ownership_scope
from tests.test_document_live_lane import cut_at


@pytest.mark.asyncio
async def test_legacy_http_save_cannot_report_an_edit_the_lane_overwrites(ownership_scope, tmp_path, monkeypatch):
    from api import files
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    user = {"id": scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0], "name": "tester"}
    monkeypatch.setattr(files, "authenticate", lambda value: user)
    app = FastAPI()
    app.include_router(files.router)
    entered, release = asyncio.Event(), asyncio.Event()
    async def held(stage):
        if stage == "record":
            entered.set()
            await release.wait()
    cut_at(monkeypatch, held)
    call = asyncio.create_task(entry.lane.submit(Operation("replace", content="LIVE",
        actor=WriteActor(scope["owner"], "project-alpha"))))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.put("/api/files/content", params={"token": "fixture-session"},
                json={"path": str(entry.live.path), "text": "LEGACY", "project_id": "project-alpha"})
        release.set()
        assert (await call).server_saved
        assert response.status_code == 409
        assert response.json()["detail"] == {
            "code": "document_live",
            "message": "This document is being edited live. Reload to keep editing; your text is still in this tab.",
        }
        assert entry.live.path.read_text() == entry.live.text == "LIVE"
    finally:
        release.set()
        await asyncio.gather(call, return_exceptions=True)
        await manager.close()


def old_client(scope, monkeypatch):
    from api import files
    user_id = scope["db"].execute("SELECT user_id FROM threads WHERE id=?", (scope["owner"],)).fetchone()[0]
    monkeypatch.setattr(files, "authenticate", lambda value: {"id": user_id, "name": "tester"})
    app = FastAPI()
    app.include_router(files.router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def save_old(client, path, text):
    return await client.put("/api/files/content", params={"token": "fixture-session"},
                            json={"path": str(path), "text": text, "project_id": "project-alpha"})


@pytest.mark.asyncio
async def test_old_client_live_refusal_preserves_unsaved_typing(ownership_scope, tmp_path, monkeypatch):
    from tests.test_document_live_suggestions import typing
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    typing(entry, 0, insert="UNSAVED ")
    try:
        async with old_client(scope, monkeypatch) as client:
            response = await save_old(client, entry.live.path, "OLD CLIENT")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "document_live"
        assert entry.live.text == "UNSAVED a b\nsecond\n"
        assert entry.live.path.read_text() == "a b\nsecond\n"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_old_client_not_live_still_saves(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    await manager.evict(entry.artifact_id, entry, can_evict=lambda: True)
    try:
        async with old_client(scope, monkeypatch) as client:
            response = await save_old(client, entry.live.path, "LEGACY")
        assert response.status_code == 200
        assert entry.live.path.read_text() == "LEGACY"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_open_waits_for_legacy_save_that_already_passed_check(ownership_scope, tmp_path, monkeypatch):
    from doceditor import document_save
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    await manager.evict(entry.artifact_id, entry, can_evict=lambda: True)
    entered, release = asyncio.Event(), asyncio.Event()
    original = document_save._save_validated
    async def held(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(document_save, "_save_validated", held)
    opening = None
    async with old_client(scope, monkeypatch) as client:
        save = asyncio.create_task(save_old(client, entry.live.path, "LEGACY"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            opening = asyncio.create_task(manager.open(entry.artifact_id, entry.live.path))
            # The old implementation opens before the held write; admission must block it.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(opening), 0.2)
            release.set()
            assert (await save).status_code == 200
            reopened = await asyncio.wait_for(opening, 2)
            assert reopened.live.path.read_text() == "LEGACY"
            # The next operation imports the outside write against the previous mirror.
            await reopened.lane.submit(Operation("flush", actor=WriteActor(scope["owner"], "project-alpha")))
            assert reopened.live.path.read_text() == reopened.live.text == "LEGACY"
        finally:
            release.set()
            await asyncio.gather(save, *([opening] if opening else []), return_exceptions=True)
            await manager.close()
