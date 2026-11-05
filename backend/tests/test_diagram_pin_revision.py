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
from test_doc_image_region_comments import _register, _threads, _url, _mk_thread, _OWNER


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
    assert rejected.json()['kind'] == 'diagram_changed'
    assert 'This diagram changed. Place the pin again; your draft is kept.' in rejected.text
    assert set(_threads(integration_client, artifact)) == {pin['id']}
    rejected_move = integration_client.patch(_url(f"/api/comments/{pin['id']}"),
        json={'region': {'x': 0.8, 'y': 0.7}, 'diagram_source': old})
    assert rejected_move.status_code == 409
    assert rejected_move.json()['kind'] == 'diagram_changed'
    assert 'This diagram changed. Place the pin again; your draft is kept.' in rejected_move.text
    unchanged = _threads(integration_client, artifact)[pin['id']]
    assert (unchanged['region_x'], unchanged['region_y'], unchanged['image_rev']) == (pin['region_x'], pin['region_y'], pin['image_rev'])

    accepted = integration_client.post(url, json={**body, 'diagram_source': new})
    assert accepted.status_code == 200, accepted.text
    moved = integration_client.patch(_url(f"/api/comments/{pin['id']}"),
        json={'region': {'x': 0.8, 'y': 0.7}, 'diagram_source': new})
    assert moved.status_code == 200, moved.text
    assert moved.json()['comment']['image_rev'] == accepted.json()['comment']['image_rev']


def test_person_diagram_pin_stays_on_its_diagram(integration_client, integration_workspace):
    path = integration_workspace / 'diagram-reanchor.md'
    text = '```mermaid\ngraph TD\n```\nWords\n```mermaid\ngraph LR\n```\n'
    path.write_text(text)
    artifact = _register(integration_client, path)
    created = integration_client.post(_url(f'/api/artifacts/{artifact}/comments'), json={
        'delivery_policy': 'immediate', 'body': 'Pin', 'anchor_text': '```mermaid',
        'from_off': 0, 'to_off': 10, 'region': {'x': .2, 'y': .3}, 'diagram_source': 'graph TD',
    })
    assert created.status_code == 200, created.text
    pin = created.json()['comment']['id']
    for start in (text.index('Words'), text.rindex('```mermaid')):
        response = integration_client.patch(_url(f'/api/comments/{pin}'), json={
            'anchor_text': text[start:start + 10], 'from_off': start, 'to_off': start + 10,
        })
        assert response.status_code == 400, response.text
        assert 'stays on its diagram' in response.text
    assert _threads(integration_client, artifact)[pin]['from_off'] == 0
    path.write_text('Intro\n' + text)
    shifted = integration_client.patch(_url(f'/api/comments/{pin}'), json={
        'anchor_text': '```mermaid', 'from_off': 6, 'to_off': 16,
    })
    assert shifted.status_code == 200, shifted.text


@pytest.mark.asyncio
async def test_agent_diagram_pin_stays_on_its_diagram(integration_client, integration_workspace):
    from tools.documents import DocCommentUpdateTool
    path = integration_workspace / 'agent-diagram-reanchor.md'
    text = '```mermaid\ngraph TD\n```\nWords\n```mermaid\ngraph LR\n```\n'
    path.write_text(text)
    artifact = _register(integration_client, path)
    from doceditor import comments, diagram_pins
    from infra.database import get_db
    sid = _mk_thread(integration_client, _OWNER, title='Design Reviewer')
    db = get_db()
    try:
        row, _ = comments.create_with_status(artifact, body='Pin', author='Reviewer', author_kind='agent',
            author_thread_id=sid, anchor_text='```mermaid', from_off=0, to_off=10,
            region=(.2, .3, 0, 0), image_rev=diagram_pins.source_revision('graph TD'), db=db)
        pin = row['id']
    finally:
        db.close()
    for start in (text.index('Words'), text.rindex('```mermaid')):
        result = await DocCommentUpdateTool().execute(comment_id=pin, anchor_text=text[start:start + 10],
            from_off=start, to_off=start + 10, _thread_id=sid)
        assert str(result).startswith('Error:') and 'stays on its diagram' in str(result), result
    assert _threads(integration_client, artifact)[pin]['from_off'] == 0


@pytest.mark.parametrize(('opening', 'body', 'closing'), [
    ('> ```mermaid', '> graph TD\n> A-->B', '> ```'),
    ('- ```mermaid', '  graph TD\n  A-->B', '  ```'),
    ('1. ```mermaid', '   graph TD\n   A-->B', '   ```'),
])
def test_nested_diagram_accepts_pin(integration_client, integration_workspace, opening, body, closing):
    path = integration_workspace / 'nested-diagram.md'
    path.write_text(f'{opening}\n{body}\n{closing}\n')
    artifact = _register(integration_client, path)
    response = integration_client.post(_url(f'/api/artifacts/{artifact}/comments'), json={
        'delivery_policy': 'immediate', 'body': 'Pin', 'anchor_text': opening,
        'from_off': 0, 'to_off': len(opening), 'region': {'x': 0.2, 'y': 0.3},
        'diagram_source': 'graph TD\nA-->B',
    })
    assert response.status_code == 200, response.text


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
