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
            json=dict(delivery_policy="immediate", body='anchor evidence', anchor_text=quote, from_off=2, to_off=3,
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
    from doceditor import comment_create_db
    captured = []
    original_anchor = comment_create_db._anchor
    def observe_anchor(request, anchor, duplicate):
        if anchor is not None:
            captured.append(anchor)
        return original_anchor(request, anchor, duplicate)
    monkeypatch.setattr(comment_create_db, '_anchor', observe_anchor)
    # Native and agent execute from another actual event loop, HTTP runs on the owner.
    async def call():
        return await invoke(kind, ownership_scope, entry, 'reserved')
    task = asyncio.create_task(call() if kind == 'http' else asyncio.to_thread(lambda: asyncio.run(call())))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert entry.live.ranges['reserved'] == (2, 3)
        typing(entry, 0, insert='😀 ')
        assert entry.live.ranges['reserved'] == (5, 6)
        assert len(captured) == 1
        assert captured[0].text == 'a b\nsecond\n'
        assert (captured[0].start, captured[0].end) == (2, 3)
        assert await asyncio.wait_for(entry.live.commit_received(), 2)
        release.set()
        result = await task
        assert result['comment']['from_off'] == 2 and result['comment']['to_off'] == 3
        assert result['comment']['anchor_prefix'] == 'a '
        stored = ownership_scope['db'].execute(
            'SELECT s.text FROM doc_comments c JOIN doc_comment_snapshots s ON s.id=c.snapshot_id WHERE c.id=?',
            ('reserved',),
        ).fetchone()
        assert stored is not None and stored[0] == b'a b\nsecond\n'
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
async def test_reload_uses_created_snapshot_and_keeps_deleted_quote(ownership_scope, tmp_path, monkeypatch):
    from doceditor import comment_anchoring
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    try:
        typing(entry, 0, delete=len(entry.live.text), insert='a b\na b\na b\n')
        await invoke('http', ownership_scope, entry, 'deleted-on-reload')
        await manager.close()
        rows, anchored = comment_anchoring.anchored_threads(
            entry.artifact_id, str(entry.live.path), ownership_scope['db'], text='a b\na b\n',
        )
        comment = next(row for row in rows if row['id'] == 'deleted-on-reload')
        assert anchored and comment['orphaned']
        assert comment['anchor_text'] == 'b'
        stored = ownership_scope['db'].execute(
            'SELECT c.from_off, c.to_off, s.text FROM doc_comments c JOIN doc_comment_snapshots s '
            'ON s.id=c.snapshot_id WHERE c.id=?', ('deleted-on-reload',),
        ).fetchone()
        assert tuple(stored) == (2, 3, b'a b\na b\na b\n')
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_live_reload_uses_snapshot_for_ambiguous_deleted_line(ownership_scope, tmp_path, monkeypatch):
    from doceditor.live_comments import positions
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    try:
        typing(entry, 0, delete=len(entry.live.text), insert='a b\na b\na b\n')
        await invoke('http', ownership_scope, entry, 'live-reload')
        typing(entry, 0, delete=4, insert='')
        frame = await positions(entry, entry.artifact_id, reload=True)
        state = next(row for row in frame['states'] if row['id'] == 'live-reload')
        assert state['orphaned']
        assert not any(row['id'] == 'live-reload' for row in frame['ranges'])
        next_frame = await positions(entry, entry.artifact_id)
        assert next(row for row in next_frame['states'] if row['id'] == 'live-reload')['orphaned']
        assert not any(row['id'] == 'live-reload' for row in next_frame['ranges'])
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_live_comment_list_keeps_ambiguous_deleted_text_gone(ownership_scope, tmp_path, monkeypatch):
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    try:
        typing(entry, 0, delete=len(entry.live.text), insert='a b\na b\na b\n')
        await invoke('http', ownership_scope, entry, 'live-list-deleted')
        typing(entry, 0, delete=4, insert='')
        app = FastAPI()
        app.include_router(doc_comments.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get(f'/api/artifacts/{entry.artifact_id}/comments', params={'token': OWNER_TOKEN})
        assert response.status_code == 200, response.text
        comment = next(c for c in response.json()['comments'] if c['id'] == 'live-list-deleted')
        assert comment['orphaned'] and comment['anchor_text'] == 'b'
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_cold_large_document_reload_uses_snapshot(ownership_scope, tmp_path, monkeypatch):
    import time
    from doceditor import comment_anchoring
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    try:
        old = 'a b\n' + ''.join(f'line {i:05d} ' + 'x' * 29 + '\n' for i in range(5000))
        typing(entry, 0, delete=len(entry.live.text), insert=old)
        await invoke('http', ownership_scope, entry, 'large-reload')
        await manager.close()
        new = 'prefix\n' + old
        entry.live.path.write_text(new)
        started = time.perf_counter()
        rows, checked = comment_anchoring.anchored_threads(entry.artifact_id, str(entry.live.path), None)
        elapsed = time.perf_counter() - started
        row = next(row for row in rows if row['id'] == 'large-reload')
        assert checked and not row['orphaned']
        assert (row['from_off'], row['to_off']) == (9, 10)
        print(f'cold_snapshot_reload bytes={len(old.encode())} seconds={elapsed:.4f}')
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_closed_document_positions_advance_anchor_and_snapshot_together(ownership_scope, tmp_path, monkeypatch):
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    try:
        await invoke('native', ownership_scope, entry, 'client-position')
        await manager.close()
        text = 'prefix\na b\nsecond\n'
        entry.live.path.write_text(text)
        app = FastAPI()
        app.include_router(doc_comments.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(
                f'/api/artifacts/{entry.artifact_id}/comments/positions', params={'token': OWNER_TOKEN},
                json={'updates': [{'id': 'client-position', 'from_off': 9, 'to_off': 10}]},
            )
        assert response.status_code == 200, response.text
        stored = ownership_scope['db'].execute(
            'SELECT c.from_off,c.to_off,s.text FROM doc_comments c JOIN doc_comment_snapshots s '
            'ON s.id=c.snapshot_id WHERE c.id=?', ('client-position',),
        ).fetchone()
        assert tuple(stored) == (9, 10, text.encode())
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_closed_document_invalid_positions_keep_dev_response_and_clear_snapshot(ownership_scope, tmp_path, monkeypatch):
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    try:
        await invoke('native', ownership_scope, entry, 'invalid-position')
        before = tuple(ownership_scope['db'].execute(
            'SELECT from_off,to_off,snapshot_id FROM doc_comments WHERE id=?', ('invalid-position',),
        ).fetchone())
        await manager.close()
        entry.live.path.write_text('prefix\na b\nsecond\n')
        app = FastAPI()
        app.include_router(doc_comments.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(
                f'/api/artifacts/{entry.artifact_id}/comments/positions', params={'token': OWNER_TOKEN},
                json={'updates': [{'id': 'invalid-position', 'from_off': 0, 'to_off': 1}]},
            )
        assert response.status_code == 200 and response.json() == {'updated': 1}
        after = tuple(ownership_scope['db'].execute(
            'SELECT from_off,to_off,snapshot_id FROM doc_comments WHERE id=?', ('invalid-position',),
        ).fetchone())
        assert after == (0, 1, None)
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_closed_positions_preserve_dev_behavior_for_ambiguous_duplicate(ownership_scope, tmp_path, monkeypatch):
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    try:
        typing(entry, 0, delete=len(entry.live.text), insert='a b\na b\na b\n')
        await invoke('http', ownership_scope, entry, 'duplicate-position', quote='b')
        before = tuple(ownership_scope['db'].execute(
            'SELECT from_off,to_off,snapshot_id,anchor_text FROM doc_comments WHERE id=?', ('duplicate-position',),
        ).fetchone())
        assert before[3] == 'b'
        await manager.close()
        entry.live.path.write_text('a b\na b\n')
        app = FastAPI()
        app.include_router(doc_comments.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(
                f'/api/artifacts/{entry.artifact_id}/comments/positions', params={'token': OWNER_TOKEN},
                json={'updates': [{'id': 'duplicate-position', 'from_off': 2, 'to_off': 3}]},
            )
        assert response.status_code == 200 and response.json() == {'updated': 1}, response.text
        after = tuple(ownership_scope['db'].execute(
            'SELECT from_off,to_off,snapshot_id,anchor_text FROM doc_comments WHERE id=?', ('duplicate-position',),
        ).fetchone())
        assert after == (2, 3, None, 'b')
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_live_publication_advances_anchor_and_snapshot_together(ownership_scope, tmp_path, monkeypatch):
    from tools.filesystem_rw import ReadFileTool, WriteFileTool
    scope = ownership_scope
    manager, entry, context = await opened(scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    try:
        await invoke('native', scope, entry, 'published-anchor')
        path = str(entry.live.path)
        await ReadFileTool(workspace=scope['workspace']).execute(path, **context)
        text = 'prefix\na b\nsecond\n'
        result = await WriteFileTool(workspace=scope['workspace']).execute(path=path, content=text, **context)
        assert not result.startswith('Error'), result
        if entry.publisher.task:
            await entry.publisher.task
        row = scope['db'].execute(
            'SELECT c.from_off,c.to_off,s.text FROM doc_comments c JOIN doc_comment_snapshots s '
            'ON s.id=c.snapshot_id WHERE c.id=?', ('published-anchor',),
        ).fetchone()
        assert tuple(row) == (9, 10, text.encode())
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_agent_patch_advances_anchor_and_snapshot_together(ownership_scope, tmp_path, monkeypatch):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    try:
        await invoke('agent', scope, entry, 'agent-patch-anchor')
        typing(entry, 0, insert='prefix ')
        result = await delegation_docs.patch_agent_comment(
            'agent-patch-anchor', OWNER_TOKEN,
            delegation_docs.AgentCommentPatch(as_session_id=scope['owner'], from_off=9, to_off=10, anchor_text='b'))
        assert result['comment']['from_off'] == 9
        row = scope['db'].execute(
            'SELECT c.from_off,c.to_off,s.text FROM doc_comments c JOIN doc_comment_snapshots s '
            'ON s.id=c.snapshot_id WHERE c.id=?', ('agent-patch-anchor',),
        ).fetchone()
        assert tuple(row) == (9, 10, entry.live.text.encode())
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['http', 'agent'])
async def test_invalid_patch_preserves_fields_and_drops_snapshot(ownership_scope, tmp_path, monkeypatch, kind):
    scope = ownership_scope
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    try:
        await invoke(kind, scope, entry, 'invalid-patch')
        fields = {'from_off': 0, 'to_off': 1, 'anchor_text': 'wrong'}
        if kind == 'agent':
            result = await delegation_docs.patch_agent_comment(
                'invalid-patch', OWNER_TOKEN, delegation_docs.AgentCommentPatch(as_session_id=scope['owner'], **fields))
            assert result['comment']['anchor_text'] == 'wrong'
        else:
            app = FastAPI()
            app.include_router(doc_comments.router)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.patch('/api/comments/invalid-patch', params={'token': OWNER_TOKEN}, json=fields)
                assert response.status_code == 200, response.text
        row = scope['db'].execute(
            'SELECT from_off,to_off,anchor_text,snapshot_id FROM doc_comments WHERE id=?', ('invalid-patch',),
        ).fetchone()
        assert tuple(row) == (0, 1, 'wrong', None)
        assert scope['db'].execute('SELECT COUNT(*) FROM doc_comment_snapshots').fetchone()[0] == 0
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize('own_connection', [False, True])
async def test_reload_advances_surviving_anchor_and_its_version_together(ownership_scope, tmp_path, monkeypatch, own_connection):
    from doceditor import comment_anchoring
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    try:
        await invoke('http', ownership_scope, entry, 'survives-reload')
        await manager.close()
        new = 'prefix\na b\nsecond\n'
        rows, anchored = comment_anchoring.anchored_threads(
            entry.artifact_id, str(entry.live.path), None if own_connection else ownership_scope['db'], text=new,
        )
        comment = next(row for row in rows if row['id'] == 'survives-reload')
        assert anchored and not comment['orphaned']
        assert (comment['from_off'], comment['to_off']) == (9, 10)
        stored = ownership_scope['db'].execute(
            'SELECT c.from_off, c.to_off, s.text FROM doc_comments c JOIN doc_comment_snapshots s '
            'ON s.id=c.snapshot_id WHERE c.id=?', ('survives-reload',),
        ).fetchone()
        assert tuple(stored) == (9, 10, new.encode())
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_list_retries_after_a_newer_live_anchor_wins(ownership_scope, tmp_path, monkeypatch):
    """A list already reading disk cannot persist over a newer live re-anchor."""
    from doceditor import comment_anchoring
    from doceditor.quote_anchor import anchor_context

    scope = ownership_scope
    db = scope['db']
    manager, entry, _ = await opened(scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    entered, release = threading.Event(), threading.Event()
    task = None
    try:
        await invoke('native', scope, entry, 'list-race')
        artifact_id, path = entry.artifact_id, entry.live.path
        if entry.publisher.task:
            await entry.publisher.task
        await manager.evict(artifact_id, entry, can_evict=lambda: True)
        path.write_text('XX ' + path.read_text())
        original_read = comment_anchoring.read_doc_text
        def held_read(*args, **kwargs):
            text = original_read(*args, **kwargs)
            entered.set()
            assert release.wait(5)
            return text
        monkeypatch.setattr(comment_anchoring, 'read_doc_text', held_read)
        app = FastAPI()
        app.include_router(doc_comments.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            task = asyncio.create_task(client.get(f'/api/artifacts/{artifact_id}/comments', params={'token': OWNER_TOKEN}))
            assert await asyncio.to_thread(entered.wait, 5)
            reopened = await manager.open(artifact_id, path)
            typing(reopened, 0, insert='YY ')
            response = await client.patch('/api/comments/list-race', params={'token': OWNER_TOKEN},
                                          json={'from_off': 8, 'to_off': 9, 'anchor_text': 'b'})
            assert response.status_code == 200, response.text
            prefix, suffix = anchor_context(reopened.live.text, 8, 9, 'b')
            paired = db.execute(
                'SELECT c.snapshot_id, s.text FROM doc_comments c LEFT JOIN doc_comment_snapshots s '
                'ON s.id=c.snapshot_id WHERE c.id=?', ('list-race',),
            ).fetchone()
            snapshot_id = paired[0]
            assert paired[1] == reopened.live.text.encode()
            release.set()
            result = await task
            assert result.status_code == 200, result.text
            listed = next(c for c in result.json()['comments'] if c['id'] == 'list-race')
            assert result.json()['anchors_checked'] and (listed['from_off'], listed['to_off']) == (8, 9)
        row = comments.get_comment('list-race', db=db)
        assert (row['from_off'], row['to_off'], row['snapshot_id'], row['anchor_text'],
                row['anchor_prefix'], row['anchor_suffix']) == (8, 9, snapshot_id, 'b', prefix, suffix)
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_mismatched_snapshot_read_preserves_legacy_result_and_clears_reference(ownership_scope, tmp_path, monkeypatch):
    from doceditor import comment_anchoring
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    try:
        await invoke('http', ownership_scope, entry, 'mismatched-read')
        await manager.close()
        db = ownership_scope['db']
        db.execute('UPDATE doc_comments SET from_off=0,to_off=1 WHERE id=?', ('mismatched-read',))
        db.commit()
        text = 'prefix\na b\nsecond\n'
        source = comments.list_for_artifact(entry.artifact_id, db=db)
        expected = [dict(item) for item in source]
        comment_anchoring.resolve_threads(expected, text)
        actual, anchored = comment_anchoring.anchored_threads(entry.artifact_id, str(entry.live.path), db, text=text)
        assert anchored
        for key in ('from_off', 'to_off', 'anchor_text', 'anchor_prefix', 'anchor_suffix', 'orphaned'):
            assert actual[0].get(key) == expected[0].get(key)
        assert db.execute('SELECT snapshot_id FROM doc_comments WHERE id=?', ('mismatched-read',)).fetchone()[0] is None
        assert db.execute('SELECT COUNT(*) FROM doc_comment_snapshots').fetchone()[0] == 0
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_stale_snapshot_update_loses_without_changing_any_pair(ownership_scope, tmp_path, monkeypatch):
    from doceditor import snapshot_reanchor
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    try:
        await invoke('http', ownership_scope, entry, 'stale-anchor')
        await manager.close()
        db = ownership_scope['db']
        rows = comments.list_for_artifact(entry.artifact_id, db=db)
        _, updates, invalid = snapshot_reanchor.resolve_versioned(rows, 'prefix\na b\nsecond\n', db)
        assert not invalid
        db.execute('UPDATE doc_comments SET from_off=0, to_off=1 WHERE id=?', ('stale-anchor',))
        db.commit()
        with pytest.raises(snapshot_reanchor.AnchorConflict):
            snapshot_reanchor.persist_updates(db, entry.artifact_id, 'prefix\na b\nsecond\n', updates)
        stored = db.execute('SELECT from_off, to_off, snapshot_id FROM doc_comments WHERE id=?',
                            ('stale-anchor',)).fetchone()
        assert tuple(stored) == (0, 1, updates[0]['expected_snapshot_id'])
        assert db.execute('SELECT COUNT(*) FROM doc_comment_snapshots').fetchone()[0] == 1
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_snapshot_mapping_cache_does_not_prevent_retry_after_failed_write(ownership_scope, tmp_path, monkeypatch):
    import sqlite3
    from doceditor import comment_anchoring, snapshot_reanchor
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    try:
        await invoke('http', ownership_scope, entry, 'cache-retry')
        await manager.close()
        db = ownership_scope['db']
        current = 'prefix\na b\nsecond\n'
        persist = snapshot_reanchor.persist_updates
        calls = []
        def fail_once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError('injected write failure')
            return persist(*args, **kwargs)
        monkeypatch.setattr(snapshot_reanchor, 'persist_updates', fail_once)
        first, anchored = comment_anchoring.anchored_threads(entry.artifact_id, str(entry.live.path), db, text=current)
        assert anchored
        original_source = db.execute('SELECT snapshot_id FROM doc_comments WHERE id=?', ('cache-retry',)).fetchone()[0]
        second, anchored = comment_anchoring.anchored_threads(entry.artifact_id, str(entry.live.path), db, text=current)
        assert anchored and len(calls) == 2
        assert (first[0]['from_off'], first[0]['to_off']) == (second[0]['from_off'], second[0]['to_off'])
        new_source = db.execute('SELECT snapshot_id FROM doc_comments WHERE id=?', ('cache-retry',)).fetchone()[0]
        assert new_source != original_source
        assert snapshot_reanchor.comment_snapshots.read_many(db, [new_source])[new_source] == current
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_snapshot_updates_verify_all_rows_in_one_query(ownership_scope, tmp_path, monkeypatch):
    from doceditor import snapshot_reanchor
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    try:
        await invoke('http', ownership_scope, entry, 'first-anchor')
        await invoke('http', ownership_scope, entry, 'second-anchor')
        await manager.close()
        db = ownership_scope['db']
        rows = comments.list_for_artifact(entry.artifact_id, db=db)
        _, updates, invalid = snapshot_reanchor.resolve_versioned(rows, 'prefix\na b\nsecond\n', db)
        assert not invalid and len(updates) == 2
        statements = []
        db.set_trace_callback(statements.append)
        snapshot_reanchor.persist_updates(db, entry.artifact_id, 'prefix\na b\nsecond\n', updates)
        checks = [s for s in statements if s.lstrip().upper().startswith('SELECT') and 'snapshot_id' in s and 'doc_comments' in s]
        assert len(checks) == 1
    finally:
        ownership_scope['db'].set_trace_callback(None)
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
async def test_snapshot_and_comment_roll_back_together(ownership_scope, tmp_path, monkeypatch):
    from doceditor import comment_create_db
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    def fail_after_snapshot(*_args):
        raise RuntimeError('controlled failure after snapshot')
    monkeypatch.setattr(comment_create_db, '_record_tags', fail_after_snapshot)
    try:
        with pytest.raises(RuntimeError, match='controlled failure'):
            await invoke('http', ownership_scope, entry, 'rollback-snapshot')
        db = ownership_scope['db']
        assert db.execute('SELECT COUNT(*) FROM doc_comments WHERE id=?', ('rollback-snapshot',)).fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM doc_comment_snapshots').fetchone()[0] == 0
        assert 'rollback-snapshot' not in entry.live.ranges
    finally:
        await manager.close()
        app_loop.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['native', 'agent'])
async def test_unopened_document_creation_stores_the_source_version(ownership_scope, tmp_path, monkeypatch, kind):
    from doceditor import live_runtime
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    await manager.close()
    monkeypatch.setattr(live_runtime, '_manager', None)
    result = await invoke(kind, ownership_scope, entry, 'on-disk')
    assert result['comment']['from_off'] == 2
    stored = ownership_scope['db'].execute(
        'SELECT s.text FROM doc_comments c JOIN doc_comment_snapshots s ON s.id=c.snapshot_id WHERE c.id=?',
        ('on-disk',),
    ).fetchone()
    assert stored is not None and stored[0] == b'a b\nsecond\n'


@pytest.mark.asyncio
async def test_unopened_invalid_selection_keeps_legacy_comment(ownership_scope, tmp_path, monkeypatch, caplog):
    caplog.set_level('INFO', logger='doceditor.comment_snapshots')
    from doceditor import live_runtime
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    await manager.close()
    monkeypatch.setattr(live_runtime, '_manager', None)
    app = FastAPI()
    app.include_router(doc_comments.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post(
            f'/api/artifacts/{entry.artifact_id}/comments', params={'token': OWNER_TOKEN},
            json={'delivery_policy': 'immediate', 'body': 'kept draft', 'anchor_text': 'wrong',
                  'from_off': 2, 'to_off': 3, 'client_id': 'invalid-on-disk'},
        )
    assert response.status_code == 200, response.text
    row = ownership_scope['db'].execute(
        'SELECT body,anchor_text,from_off,to_off,anchor_prefix,anchor_suffix,snapshot_id '
        'FROM doc_comments WHERE id=?', ('invalid-on-disk',),
    ).fetchone()
    assert tuple(row) == ('kept draft', 'wrong', 2, 3, None, None, None)
    assert 'quote_mismatch' in caplog.text and 'invalid-on-disk' in caplog.text


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
        typing(entry, 2, delete=1, insert='')
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


@pytest.mark.asyncio
async def test_refused_owner_create_reaches_its_caller_and_is_logged_once(ownership_scope, tmp_path, monkeypatch):
    """An overlapping suggestion is refused to the agent that asked; the owner-loop task adds no second ERROR."""
    from fastapi import HTTPException
    from loguru import logger

    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    logged: list[str] = []
    sink = logger.add(lambda message: logged.append(str(message)), level='ERROR')

    def suggest(identifier):
        return delegation_docs.create_agent_comment(OWNER_TOKEN, delegation_docs.AgentCommentCreate(
            body='tighten', as_session_id=ownership_scope['owner'], artifact_id=entry.artifact_id,
            quote='b', replacement='c', client_id=identifier))

    try:
        await suggest('first')
        with pytest.raises(HTTPException) as refused:
            await suggest('second')
        await asyncio.sleep(0.05)
        assert refused.value.status_code == 409 and 'overlaps an open suggestion' in refused.value.detail
        assert logged == []
    finally:
        logger.remove(sink)
        await manager.close()
        app_loop.clear()
