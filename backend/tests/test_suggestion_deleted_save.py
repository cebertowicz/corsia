"""A deleted document cannot be recreated by accepting a suggestion."""

import pytest

from delegation_docs_fixtures import _OWNER, _mk_thread, _registrable_roots  # noqa: F401
from test_doc_suggestions import _own, _row, _suggest, _user_decide

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize('timing', ['before', 'publication'])
async def test_deleted_document_refuses_decision_through_editor_route(
    integration_client, integration_workspace, _registrable_roots, monkeypatch, timing,
):
    from registry import durable_writes

    shared = integration_workspace / 'shared' / 'planning'
    shared.mkdir(parents=True, exist_ok=True)
    (shared.parent / 'INDEX.md').write_text('# Shared\n')
    (shared / 'INDEX.md').write_text('# Planning\n')
    path = shared / f'proposal-{timing}.md'
    quote = f'A unique {timing} sentence'
    path.write_text(f'{quote}\n')
    await _own(integration_client, path)
    author = _mk_thread(integration_client, _OWNER)
    suggestion = await _suggest(author, path, quote=quote)
    staged = []
    if timing == 'before':
        path.unlink()
    else:
        original = durable_writes.stage_bound_bytes

        def delete_after_stage(target, *args, **kwargs):
            result = original(target, *args, **kwargs)
            if target == path:
                staged.append(result)
                path.unlink()
            return result

        monkeypatch.setattr(durable_writes, 'stage_bound_bytes', delete_after_stage)

    response = _user_decide(integration_client, suggestion['id'], 'accept')

    assert response.status_code == 404, response.text
    assert response.json()['detail'] == "This file no longer exists, so nothing was changed. You can close it."
    assert not path.exists()
    assert _row(suggestion['id'])['decision'] is None
    if timing == 'publication':
        assert staged and all(not stage.exists() for stage in staged)
