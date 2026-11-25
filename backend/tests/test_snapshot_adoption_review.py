"""Regression tests for adopting legacy anchors only on document writes."""
import asyncio

import httpx
import pytest
from fastapi import FastAPI

from api import doc_comments
from auth_fixtures import OWNER_TOKEN

from doceditor import comment_anchoring, comments
from infra import app_loop
from tests.test_document_live_file_tools import opened
from tests.test_live_comment_creation import invoke
from tests.test_shared_document_ownership import ownership_scope as ownership_scope
from tools.filesystem_rw import ReadFileTool, WriteFileTool


@pytest.mark.asyncio
async def test_create_adopts_valid_legacy_and_leaves_invalid_on_legacy(ownership_scope, tmp_path, monkeypatch):
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    db = ownership_scope['db']
    try:
        await invoke('native', ownership_scope, entry, 'legacy-valid')
        await invoke('native', ownership_scope, entry, 'legacy-invalid')
        db.execute("UPDATE doc_comments SET snapshot_id=NULL WHERE id IN ('legacy-valid','legacy-invalid')")
        db.execute("UPDATE doc_comments SET from_off=0,to_off=1 WHERE id='legacy-invalid'")
        db.commit()
        text = entry.live.text
        expected = comments.list_for_artifact(entry.artifact_id, db=db)
        comment_anchoring.resolve_threads(expected, text)
        await invoke('native', ownership_scope, entry, 'new-comment')
        rows = {r['id']: r for r in comments.list_for_artifact(entry.artifact_id, db=db)}
        assert rows['legacy-valid']['snapshot_id'] is not None
        assert rows['legacy-invalid']['snapshot_id'] is None
        invalid = dict(rows['legacy-invalid'])
        comment_anchoring.resolve_threads([invalid], text)
        reference = next(r for r in expected if r['id'] == 'legacy-invalid')
        assert invalid == reference
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_client_batch_maps_one_version_once_outside_write_transaction(ownership_scope, tmp_path, monkeypatch):
    from doceditor import snapshot_reanchor, snapshot_showing
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    try:
        for index in range(20):
            await invoke('native', ownership_scope, entry, f'batch-{index}')
        await manager.close()
        entry.live.path.write_text('prefix\na b\nsecond\n')
        original_resolve = snapshot_reanchor.resolve_versioned
        def checked_resolve(rows, text, db):
            assert not db.in_transaction
            return original_resolve(rows, text, db)
        monkeypatch.setattr(snapshot_reanchor, 'resolve_versioned', checked_resolve)
        calls = []
        original = snapshot_showing._line_pairs
        def counted(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)
        monkeypatch.setattr(snapshot_showing, '_line_pairs', counted)
        app = FastAPI()
        app.include_router(doc_comments.router)
        updates = [{'id': f'batch-{i}', 'from_off': 9, 'to_off': 10} for i in range(20)]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(f'/api/artifacts/{entry.artifact_id}/comments/positions',
                                         params={'token': OWNER_TOKEN}, json={'updates': updates})
        assert response.status_code == 200 and response.json() == {'updated': 20}, response.text
        assert len(calls) == 1
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_client_positions_adopt_valid_legacy_after_update(ownership_scope, tmp_path, monkeypatch):
    from doceditor import snapshot_reanchor
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    db = ownership_scope['db']
    try:
        await invoke('native', ownership_scope, entry, 'positions-legacy')
        db.execute("UPDATE doc_comments SET snapshot_id=NULL WHERE id='positions-legacy'")
        db.commit()
        await manager.close()
        app = FastAPI()
        app.include_router(doc_comments.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(f'/api/artifacts/{entry.artifact_id}/comments/positions',
                                         params={'token': OWNER_TOKEN},
                                         json={'updates': [{'id': 'positions-legacy', 'from_off': 2, 'to_off': 3}]})
        assert response.status_code == 200, response.text
        row = comments.get_comment('positions-legacy', db=db)
        assert row['snapshot_id'] is not None
        snapshot_reanchor.resolve_versioned([row], 'second\n', db)
        assert row['orphaned'] and row['anchor_text'] == 'b'
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_client_adoption_does_not_commit_stale_document(ownership_scope, tmp_path, monkeypatch):
    from doceditor import comment_snapshots
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    db = ownership_scope['db']
    try:
        await invoke('native', ownership_scope, entry, 'stale-legacy')
        db.execute("UPDATE doc_comments SET snapshot_id=NULL WHERE id='stale-legacy'")
        db.commit()
        await manager.close()
        def changed():
            raise RuntimeError('document changed')
        comment_snapshots.adopt_before_write(None, before=entry.live.text, artifact_id=entry.artifact_id,
                                             verify_text=changed)
        assert comments.get_comment('stale-legacy', db=db)['snapshot_id'] is None
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_client_batch_refuses_snapshot_collected_during_mapping(ownership_scope, tmp_path, monkeypatch):
    from doceditor import comment_snapshots
    from infra.database import write_transaction

    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    db = ownership_scope['db']
    try:
        await invoke('native', ownership_scope, entry, 'collected-race')
        await manager.close()
        original = comment_snapshots.read_many

        def collect_before_read(connection, ids):
            old_id = next(iter(ids))
            with write_transaction(db, scope='test_collect_race'):
                replacement = comment_snapshots.intern(db, 'newer version')
                db.execute("UPDATE doc_comments SET snapshot_id=? WHERE id='collected-race'", (replacement,))
                comment_snapshots.collect_candidates(db, [old_id])
            return original(connection, ids)

        monkeypatch.setattr(comment_snapshots, 'read_many', collect_before_read)
        app = FastAPI()
        app.include_router(doc_comments.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(f'/api/artifacts/{entry.artifact_id}/comments/positions',
                                         params={'token': OWNER_TOKEN},
                                         json={'updates': [{'id': 'collected-race', 'from_off': 2, 'to_off': 3}]})
        assert response.status_code == 409, response.text
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_client_batch_refuses_document_changed_during_mapping(ownership_scope, tmp_path, monkeypatch):
    from doceditor import snapshot_reanchor
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    try:
        await invoke('native', ownership_scope, entry, 'race')
        await manager.close()
        entry.live.path.write_text('prefix\na b\nsecond\n')
        before = comments.get_comment('race', db=ownership_scope['db'])
        original = snapshot_reanchor.resolve_versioned
        def race(*args, **kwargs):
            result = original(*args, **kwargs)
            entry.live.path.write_text('changed while mapping\n')
            return result
        monkeypatch.setattr(snapshot_reanchor, 'resolve_versioned', race)
        app = FastAPI()
        app.include_router(doc_comments.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(f'/api/artifacts/{entry.artifact_id}/comments/positions',
                                         params={'token': OWNER_TOKEN},
                                         json={'updates': [{'id': 'race', 'from_off': 9, 'to_off': 10}]})
        assert response.status_code == 409
        after = comments.get_comment('race', db=ownership_scope['db'])
        assert (after['from_off'], after['to_off'], after['snapshot_id']) == (before['from_off'], before['to_off'], before['snapshot_id'])
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_publication_does_not_rewind_anchor_changed_during_preparation(ownership_scope, tmp_path, monkeypatch):
    from doceditor import comment_snapshots, live_publisher
    from infra.database import write_transaction

    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    db = scope['db']
    try:
        await invoke('native', scope, entry, 'publisher-race')
        original = live_publisher.prepare_owner_report
        changed = {}
        def race(*args, **kwargs):
            result = original(*args, **kwargs)
            with write_transaction(db, scope='test_concurrent_anchor'):
                snapshot_id = comment_snapshots.intern(db, 'newer a b\nsecond\n')
                db.execute("UPDATE doc_comments SET from_off=8,to_off=9,snapshot_id=? WHERE id='publisher-race'", (snapshot_id,))
                comments.create_with_status(entry.artifact_id, body='new', client_id='new-during-publication',
                                            anchor_text='b', from_off=8, to_off=9, db=db)
                db.execute("UPDATE doc_comments SET snapshot_id=? WHERE id='new-during-publication'", (snapshot_id,))
            changed['snapshot_id'] = snapshot_id
            return result
        monkeypatch.setattr(live_publisher, 'prepare_owner_report', race)
        path = str(entry.live.path)
        await ReadFileTool(workspace=scope['workspace']).execute(path, **context)
        result = await WriteFileTool(workspace=scope['workspace']).execute(path=path, content='prefix\na b\nsecond\n', **context)
        assert not result.startswith('Error'), result
        if entry.publisher.task:
            await entry.publisher.task
        for identifier in ('publisher-race', 'new-during-publication'):
            row = comments.get_comment(identifier, db=db)
            assert (row['from_off'], row['to_off'], row['snapshot_id']) == (8, 9, changed['snapshot_id'])
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_closed_shared_file_write_adopts_before_deletion(ownership_scope, tmp_path, monkeypatch):
    from doceditor import snapshot_reanchor
    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    db = scope['db']
    try:
        await invoke('native', scope, entry, 'closed-legacy')
        db.execute("UPDATE doc_comments SET snapshot_id=NULL WHERE id='closed-legacy'")
        db.commit()
        path = str(entry.live.path)
        await manager.close()
        await ReadFileTool(workspace=scope['workspace']).execute(path, **context)
        result = await WriteFileTool(workspace=scope['workspace']).execute(path=path, content='second\n', **context)
        assert not result.startswith('Error'), result
        row = comments.get_comment('closed-legacy', db=db)
        assert row['snapshot_id'] is not None
        snapshot_reanchor.resolve_versioned([row], 'second\n', db)
        assert row['orphaned'] and row['anchor_text'] == 'b'
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_adoption_failure_warns_and_does_not_block_write(ownership_scope, tmp_path, monkeypatch, caplog):
    from doceditor import comment_snapshots
    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    db = scope['db']
    try:
        await invoke('native', scope, entry, 'adoption-failure')
        db.execute("UPDATE doc_comments SET snapshot_id=NULL WHERE id='adoption-failure'")
        db.commit()
        path = str(entry.live.path)
        await manager.close()
        await ReadFileTool(workspace=scope['workspace']).execute(path, **context)
        def fail(*args, **kwargs):
            raise RuntimeError('controlled adoption failure')
        monkeypatch.setattr(comment_snapshots, 'adopt_legacy', fail)
        result = await WriteFileTool(workspace=scope['workspace']).execute(path=path, content='second\n', **context)
        assert not result.startswith('Error'), result
        assert entry.live.path.read_text() == 'second\n'
        assert comments.get_comment('adoption-failure', db=db)['snapshot_id'] is None
        assert 'Could not adopt legacy comment anchors before write' in caplog.text
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize('writer', ['file_tool', 'document_save', 'knowledge', 'guidance'])
async def test_closed_ordinary_write_adopts_before_deletion(ownership_scope, tmp_path, monkeypatch, writer):
    from doceditor import snapshot_reanchor
    from doceditor.document_save import DocumentSave, _write_document
    from registry import service

    scope = ownership_scope
    manager, _, context = await opened(scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    db = scope['db']
    try:
        if writer == 'knowledge':
            from memory import knowledge_base
            monkeypatch.setattr(knowledge_base, 'KB_ROOT', scope['root'] / '.threads' / scope['owner'] / 'kb')
            path = knowledge_base.KB_ROOT / 'facts' / 'ordinary.md'
        else:
            path = scope['root'] / '.threads' / scope['owner'] / 'ordinary.md'
        _write_document(DocumentSave(str(path), 'a b\nsecond\n', project_id='project-alpha',
                                     thread_id=scope['owner'], create=True), path, direct_user=False)
        artifact = service.find_file_identity(str(path), db=db)
        entry = await manager.open(artifact['id'], path)
        await invoke('native', scope, entry, 'ordinary-legacy')
        db.execute("UPDATE doc_comments SET snapshot_id=NULL WHERE id='ordinary-legacy'")
        db.commit()
        await manager.close()
        if writer == 'file_tool':
            await ReadFileTool(workspace=scope['workspace']).execute(str(path), **context)
            result = await WriteFileTool(workspace=scope['workspace']).execute(
                path=str(path), content='second\n', **context)
            assert not result.startswith('Error'), result
        elif writer == 'document_save':
            _write_document(DocumentSave(str(path), 'second\n', project_id='project-alpha',
                                         thread_id=scope['owner']), path, direct_user=False)
        elif writer == 'knowledge':
            knowledge_base.write_entry('ordinary', 'second', 'facts')
        else:
            from infra.guidance_history_paths import write_bytes
            write_bytes(path, b'second\n')
        row = comments.get_comment('ordinary-legacy', db=db)
        assert row['snapshot_id'] is not None
        snapshot_reanchor.resolve_versioned([row], path.read_text(), db)
        assert row['orphaned'] and row['anchor_text'] == 'b'
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_closed_write_does_not_adopt_anchor_changed_during_publication(ownership_scope, tmp_path, monkeypatch):
    from registry import durable_writes

    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    db = scope['db']
    try:
        await invoke('native', scope, entry, 'changed-during-write')
        path = str(entry.live.path)
        await manager.close()
        await ReadFileTool(workspace=scope['workspace']).execute(path, **context)
        original = durable_writes._register_applied

        def advance_anchor(*args, **kwargs):
            db.execute("UPDATE doc_comments SET snapshot_id=NULL WHERE id='changed-during-write'")
            db.commit()
            return original(*args, **kwargs)

        monkeypatch.setattr(durable_writes, '_register_applied', advance_anchor)
        result = await WriteFileTool(workspace=scope['workspace']).execute(
            path=path, content='a b\nnew text\n', **context)
        assert not result.startswith('Error'), result
        assert comments.get_comment('changed-during-write', db=db)['snapshot_id'] is None
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_first_save_deleting_legacy_anchor_retains_its_source(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    db = scope['db']
    try:
        path = str(entry.live.path)
        await ReadFileTool(workspace=scope['workspace']).execute(path, **context)
        initial = await WriteFileTool(workspace=scope['workspace']).execute(path=path, content='a b\nother\na b\n', **context)
        assert not initial.startswith('Error'), initial
        if entry.publisher.task:
            await entry.publisher.task
        await invoke('http', scope, entry, 'delete-first')
        await invoke('http', scope, entry, 'already-versioned')
        db.execute("UPDATE doc_comments SET snapshot_id=NULL WHERE id='delete-first'")
        db.commit()
        before = comments.get_comment('already-versioned', db=db)
        await ReadFileTool(workspace=scope['workspace']).execute(path, **context)
        result = await WriteFileTool(workspace=scope['workspace']).execute(path=path, content='other\na b\n', **context)
        assert not result.startswith('Error'), result
        if entry.publisher.task:
            await entry.publisher.task
        from doceditor import snapshot_reanchor
        rows = [comments.get_comment(identifier, db=db) for identifier in ('delete-first', 'already-versioned')]
        assert all(row['snapshot_id'] is not None for row in rows)
        assert all((row['from_off'], row['to_off']) == (before['from_off'], before['to_off']) for row in rows)
        snapshot_reanchor.resolve_versioned(rows, 'other\na b\n', db)
        assert all(row['orphaned'] and row['anchor_text'] == 'b' for row in rows)
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize('demoted', [False, True], ids=['legacy', 'demoted'])
async def test_save_adopts_before_pairing_and_later_delete_stays_gone(ownership_scope, tmp_path, monkeypatch, demoted):
    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    db = scope['db']
    try:
        await invoke('native', scope, entry, 'old-comment')
        if demoted:
            from doceditor import comment_snapshots
            from infra.database import write_transaction
            with write_transaction(db, scope='test_demote'):
                comment_snapshots.pair_published_positions(db, entry.artifact_id, 'different text')
        else:
            db.execute("UPDATE doc_comments SET snapshot_id=NULL WHERE id='old-comment'")
            db.commit()
        assert comments.get_comment('old-comment', db=db)['snapshot_id'] is None
        path = str(entry.live.path)
        await ReadFileTool(workspace=scope['workspace']).execute(path, **context)
        original = entry.live.text
        result = await WriteFileTool(workspace=scope['workspace']).execute(path=path, content=original + 'tail\n', **context)
        assert not result.startswith('Error'), result
        if entry.publisher.task:
            await entry.publisher.task
        row = comments.get_comment('old-comment', db=db)
        assert row['snapshot_id'] is not None
        from doceditor import snapshot_reanchor
        check = dict(row)
        snapshot_reanchor.resolve_versioned([check], 'a\nsecond\ntail\n', db)
        assert check['orphaned'] and check['anchor_text'] == 'b'
    finally:
        await manager.close()
        app_loop.clear()
