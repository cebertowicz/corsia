"""A diagram pin is never written against a source other than the one displayed."""

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from api import doc_comments
from auth_fixtures import OWNER_TOKEN
from infra import app_loop
from tests.test_document_live_file_tools import opened
from tests.test_document_live_suggestions import typing, user_id
from tests.test_shared_document_ownership import ownership_scope as ownership_scope
from delegation_docs_fixtures import _registrable_roots  # noqa: F401
from test_doc_image_region_comments import _register, _threads, _url


def test_changed_diagram_refuses_new_and_moved_pins(integration_client, integration_workspace):
    old, new = 'graph TD\n A-->B', 'graph TD\n A-->C'
    path = integration_workspace / 'diagram-revision.md'
    path.write_text(f'```mermaid\n{old}\n```\n')
    artifact = _register(integration_client, path)
    url = _url(f'/api/artifacts/{artifact}/comments')
    body = {'delivery_policy': 'immediate', 'body': 'Pin', 'anchor_text': '```mermaid',
            'from_off': 0, 'to_off': 10, 'region': {'x': 0.2, 'y': 0.3}, 'diagram_source': old}
    created = integration_client.post(url, json=body)
    assert created.status_code == 200, created.text
    pin = created.json()['comment']
    path.write_text(f'```mermaid\n{new}\n```\n')

    rejected = integration_client.post(url, json={**body, 'body': 'Old snapshot'})
    assert rejected.status_code == 409
    assert set(_threads(integration_client, artifact)) == {pin['id']}
    rejected_move = integration_client.patch(_url(f"/api/comments/{pin['id']}"),
        json={'region': {'x': 0.8, 'y': 0.7}, 'diagram_source': old})
    assert rejected_move.status_code == 409
    unchanged = _threads(integration_client, artifact)[pin['id']]
    assert (unchanged['region_x'], unchanged['region_y'], unchanged['image_rev']) == (pin['region_x'], pin['region_y'], pin['image_rev'])

    accepted = integration_client.post(url, json={**body, 'diagram_source': new})
    assert accepted.status_code == 200, accepted.text
    moved = integration_client.patch(_url(f"/api/comments/{pin['id']}"),
        json={'region': {'x': 0.8, 'y': 0.7}, 'diagram_source': new})
    assert moved.status_code == 200, moved.text
    assert moved.json()['comment']['image_rev'] == accepted.json()['comment']['image_rev']


@pytest.mark.asyncio
async def test_live_edit_refuses_pin_on_viewers_previous_snapshot(ownership_scope, tmp_path, monkeypatch):
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    old, new = 'graph TD\n A-->B', 'graph TD\n A-->C'
    typing(entry, 0, insert=f'```mermaid\n{old}\n```\n')
    live = entry.live
    assert live.reserve('old-view', 0, 10, user_id(ownership_scope), object(), vector=live.doc.get_state(),
                        digest=live.digest, length=len(live.text.encode('utf-16-le')) // 2)
    typing(entry, live.text.index('A-->B'), delete=5, insert='A-->C')
    assert old not in live.text and new in live.text
    app_loop.capture(asyncio.get_running_loop())
    app = FastAPI()
    app.include_router(doc_comments.router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(f'/api/artifacts/{entry.artifact_id}/comments', params={'token': OWNER_TOKEN},
                json={'delivery_policy': 'immediate', 'body': 'Unseen', 'anchor_text': '```mermaid',
                      'from_off': 0, 'to_off': 10, 'live_capture': True, 'client_id': 'old-view',
                      'region': {'x': 0.2, 'y': 0.3}, 'diagram_source': old})
        assert response.status_code == 409, response.text
        from doceditor import comments
        assert comments.get_comment('old-view', db=ownership_scope['db']) is None
    finally:
        await manager.close()
        app_loop.clear()
