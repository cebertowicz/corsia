"""Production adapters reserve on the actual live owner before real DB commit."""
import asyncio
import base64
import json
import threading

import httpx
import pytest
from fastapi import FastAPI

from api import delegation_docs, doc_comments
from auth_fixtures import OWNER_TOKEN
from doceditor import comments
from infra import app_loop
from infra.db_async import run_in_db
from tests.test_document_live_file_tools import opened
from tests.test_document_live_suggestions import typing, user_id
from tests.test_shared_document_ownership import ownership_scope as ownership_scope
from tools.documents import DocCommentCreateTool


def snapshot(entry):
    live = entry.live
    return dict(store_id=live.store.store_id, vector=base64.b64encode(live.doc.get_state()).decode(),
                digest=live.digest, text_length_utf16=len(live.text.encode('utf-16-le')) // 2)


def reserve(scope, entry, identifier, start=2, end=3):
    """The capture a browser makes before it posts: its selection tracked under the comment's own client id."""
    live = entry.live
    assert live.reserve(identifier, start, end, user_id(scope), object(), vector=live.doc.get_state(),
                        digest=live.digest, length=len(live.text.encode('utf-16-le')) // 2)


async def invoke(kind, scope, entry, identifier, quote='b'):
    if kind == 'native':
        value = await DocCommentCreateTool(workspace=scope['workspace']).execute(
            body='anchor evidence', _thread_id=scope['owner'], artifact_id=entry.artifact_id,
            quote=quote, client_id=identifier)
        return json.loads(value) if not value.startswith('Error') else value
    if kind == 'agent':
        return await delegation_docs.create_agent_comment(OWNER_TOKEN, delegation_docs.AgentCommentCreate(
            body='anchor evidence', as_session_id=scope['owner'], artifact_id=entry.artifact_id,
            quote=quote, client_id=identifier))
    if identifier not in entry.live.ranges:
        reserve(scope, entry, identifier)
    app = FastAPI()
    app.include_router(doc_comments.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post(f'/api/artifacts/{entry.artifact_id}/comments', params={'token': OWNER_TOKEN},
            json=dict(body='anchor evidence', anchor_text=quote, from_off=2, to_off=3,
                      live_capture=True, client_id=identifier))
        assert response.status_code == 200, response.text
        return response.json()


def hold(monkeypatch, identifier, fail=False, after_commit=False):
    entered, release = threading.Event(), threading.Event()
    original = comments.create_with_status
    def create(*args, **kwargs):
        if kwargs.get('client_id') == identifier:
            entered.set()
            assert release.wait(5)
            if fail:
                raise OSError('controlled rollback')
        return original(*args, **kwargs)
    monkeypatch.setattr(comments, 'create_with_status', create)
    return entered, release


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['native', 'agent', 'http'])
async def test_owner_reservation_maps_prefix_and_browser_durability_while_db_held(ownership_scope, tmp_path, monkeypatch, kind):
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    entered, release = hold(monkeypatch, 'reserved')
    # Native and agent execute from another actual event loop, HTTP runs on the owner.
    async def call():
        return await invoke(kind, ownership_scope, entry, 'reserved')
    task = asyncio.create_task(call() if kind == 'http' else asyncio.to_thread(lambda: asyncio.run(call())))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert entry.live.ranges['reserved'] == (2, 3)
        typing(entry, 0, insert='😀 ')
        assert entry.live.ranges['reserved'] == (5, 6)
        assert await asyncio.wait_for(entry.live.commit_received(), 2)
        release.set()
        result = await task
        assert result['comment']['from_off'] == 2 and result['comment']['to_off'] == 3
        assert result['comment']['anchor_prefix'] == 'a '
        assert entry.live.range_text('reserved') == 'b'
        mapped = entry.live.ranges['reserved']
        # A deleted agent quote and an already spent reservation cannot defeat idempotency.
        await invoke(kind, ownership_scope, entry, 'reserved', quote='missing' if kind != 'http' else 'b')
        assert entry.live.ranges['reserved'] == mapped
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['native', 'agent', 'http'])
@pytest.mark.parametrize('rollback', [False, True])
async def test_repeated_cancel_joins_commit_or_proven_rollback(ownership_scope, tmp_path, monkeypatch, kind, rollback):
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    entered, release = hold(monkeypatch, 'cancelled', fail=rollback)
    task = asyncio.create_task(invoke(kind, ownership_scope, entry, 'cancelled'))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        typing(entry, 2, delete=1, insert='b')
        assert entry.live.ranges['cancelled'][0] == entry.live.ranges['cancelled'][1]
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        row = await run_in_db(comments.get_comment, 'cancelled')
        assert (row is None) == rollback
        assert ('cancelled' not in entry.live.ranges) == rollback
        if not rollback:
            assert entry.live.range_text('cancelled') == ''
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await manager.close()
        app_loop.clear()
