"""A live suggestion decision refuses deletion through the editor endpoint."""

import httpx
import pytest
from fastapi import FastAPI

from doceditor import comments
from tests.test_document_live_file_tools import opened
from tests.test_document_live_suggestions import suggestion, user_id
from tests.test_shared_document_ownership import ownership_scope as ownership_scope  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize('timing', ['before', 'publication'])
async def test_live_deleted_document_refuses_decision(ownership_scope, tmp_path, monkeypatch, timing):
    from api import doc_comments
    from tools import filesystem_effects

    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    row = suggestion(scope, entry)
    path = entry.live.path
    monkeypatch.setattr(doc_comments, 'authenticate', lambda _token: {'id': user_id(scope), 'name': 'user'})
    staged = []
    if timing == 'before':
        path.unlink()
    else:
        original = filesystem_effects.stage_bound_bytes

        def delete_after_stage(target, *args, **kwargs):
            result = original(target, *args, **kwargs)
            if target == path:
                staged.append(result)
                path.unlink()
            return result

        monkeypatch.setattr(filesystem_effects, 'stage_bound_bytes', delete_after_stage)
    app = FastAPI()
    app.include_router(doc_comments.router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(f'/api/comments/{row["id"]}/decision', params={'token': 'test'}, json={'decision': 'accept'})
        assert response.status_code == 404, response.text
        assert response.json()['detail'].startswith('No such file: ')
        assert not path.exists()
        assert comments.get_comment(row['id'], db=scope['db'])['decision'] is None
        if timing == 'publication':
            assert staged and all(not stage.exists() for stage in staged)
    finally:
        await manager.close()
