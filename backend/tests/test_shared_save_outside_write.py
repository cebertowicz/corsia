"""A shared document save must not replace an outside edit made after its hash check."""

import asyncio
import hashlib

import pytest

from test_durable_writes import durable_scope  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize('outside_content', ['outside edit', None], ids=['edit', 'deletion'])
async def test_outside_edit_between_hash_check_and_publication_survives(durable_scope, monkeypatch, outside_content):
    from doceditor.document_save import DocumentSave, DocumentSaveError, save_document_text
    from infra.guidance_targets import history_target
    from registry import durable_writes

    workspace, project_root, thread_id, db = durable_scope
    target = project_root / 'shared' / 'planning' / 'race.md'
    await asyncio.to_thread(
        durable_writes.write_text, target, 'original', thread_id=thread_id, project_id='project-alpha',
        workspace=workspace, description='Race reproduction',
    )
    assert history_target(target, workspace) is not None
    user_id = db.execute('SELECT user_id FROM threads WHERE id=?', (thread_id,)).fetchone()[0]
    stage = durable_writes.stage_bound_bytes

    def stage_with_outside_edit(*args, **kwargs):
        result = stage(*args, **kwargs)
        if outside_content is None:
            target.unlink()
        else:
            target.write_text(outside_content, encoding='utf-8')
        return result

    monkeypatch.setattr(durable_writes, 'stage_bound_bytes', stage_with_outside_edit)
    body = DocumentSave(path=str(target), text='suggestion result', project_id='project-alpha')
    expected = hashlib.sha256(b'original').hexdigest()
    with pytest.raises(DocumentSaveError) as exc:
        await save_document_text(body, user_id, expected_sha256=expected)
    assert exc.value.kind == ('no_such_file' if outside_content is None else 'stale')
    if outside_content is None:
        assert exc.value.message == 'This file no longer exists, so nothing was changed. You can close it.'
        assert not target.exists()
    else:
        assert target.read_text(encoding='utf-8') == outside_content
