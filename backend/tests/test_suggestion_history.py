"""Undo and redo an accepted suggestion through the canonical decision writer."""
import asyncio
import hashlib

import pytest

from delegation_docs_fixtures import _OTHER, _OWNER, _mk_thread, _registrable_roots, doc
from test_doc_suggestions import _own, _own_in_folder, _row, _suggest, _user_decide
from test_doc_accept_all_open import BODY, HEADING, TAIL, _accept_all, _raise_ask
from test_review_bar_http import _clear, _review

pytestmark = pytest.mark.integration


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def receipt(event):
    from infra.database import get_db

    db = get_db()
    try:
        row = db.execute('SELECT * FROM doc_suggestion_history WHERE event_id=?', (event,)).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_accept_all_returns_the_shared_decision_event(integration_client, doc, _registrable_roots):
    doc.write_text(BODY)
    owner, artifact_id = await _own(integration_client, doc)
    first = await _suggest(owner, doc, replacement='# New design', quote=HEADING)
    second = await _suggest(owner, doc, replacement='A concise tail sentence', quote=TAIL)
    result = _accept_all(integration_client, artifact_id)
    assert result.status_code == 200, result.text
    event = result.json()['decision_event_id']
    assert event and _row(first['id'])['decision_event_id'] == event
    assert _row(second['id'])['decision_event_id'] == event
    saved = receipt(event)
    assert saved is not None
    after = doc.read_text()
    assert saved['before_sha256'] == digest(BODY)
    assert saved['after_sha256'] == digest(after)
    url = f'/api/artifacts/{artifact_id}/suggestions/history'
    def undo(ids):
        return integration_client.post(url, params={'token': _OWNER}, json={
            'event_id': event, 'ids': ids, 'direction': 'undo',
            'expected_sha256': digest(after), 'text': BODY,
        })
    assert undo([first['id']]).status_code == 409
    assert doc.read_text() == after
    assert undo([first['id'], second['id']]).status_code == 200
    assert doc.read_text() == BODY
    assert _row(first['id'])['decision'] is None
    assert _row(second['id'])['decision'] is None
    redone = integration_client.post(url, params={'token': _OWNER}, json={
        'event_id': event, 'ids': [second['id'], first['id']], 'direction': 'redo',
        'expected_sha256': digest(BODY), 'text': after,
    })
    assert redone.status_code == 200, redone.text
    assert doc.read_text() == after
    assert _row(first['id'])['decision'] == _row(second['id'])['decision'] == 'accepted'


@pytest.mark.asyncio
async def test_accept_undo_redo_are_one_decision_and_document_transition(integration_client, doc, _registrable_roots):
    owner, path = await _own_in_folder(integration_client, doc)
    before = path.read_text()
    suggestion = await _suggest(owner, path)
    accepted = _user_decide(integration_client, suggestion['id'], 'accept')
    assert accepted.status_code == 200, accepted.text
    after = path.read_text()
    event = accepted.json()['comment']['decision_event_id']
    artifact = accepted.json()['comment']['artifact_id']
    saved = receipt(event)
    assert saved is not None
    assert (saved['before_sha256'], saved['after_sha256']) == (digest(before), digest(after))
    url = f'/api/artifacts/{artifact}/suggestions/history'

    def transition(direction, current, target):
        return integration_client.post(url, params={'token': _OWNER}, json={
            'event_id': event, 'ids': [suggestion['id']], 'direction': direction,
            'expected_sha256': digest(current), 'text': target,
        })

    unauthorized = integration_client.post(url, params={'token': _OTHER}, json={
        'event_id': event, 'ids': [suggestion['id']], 'direction': 'undo',
        'expected_sha256': digest(after), 'text': before,
    })
    assert unauthorized.status_code == 404
    assert path.read_text() == after
    # A matching revision does not authorize replacing the document with arbitrary text.
    forged = transition('undo', after, 'unrelated text')
    assert forged.status_code == 409
    assert path.read_text() == after
    assert _row(suggestion['id'])['decision'] == 'accepted'
    undone = transition('undo', after, before)
    assert undone.status_code == 200, undone.text
    assert path.read_text() == before
    assert _row(suggestion['id'])['decision'] is None
    # A lost answer is safe to retry: it must not apply the transition twice.
    repeated = transition('undo', after, before)
    assert repeated.status_code == 200, repeated.text
    assert path.read_text() == before
    redone = transition('redo', before, after)
    assert redone.status_code == 200, redone.text
    assert path.read_text() == after
    assert _row(suggestion['id'])['decision'] == 'accepted'
    stale = transition('undo', before, 'unrelated text')
    assert stale.status_code == 409
    assert path.read_text() == after
    assert _row(suggestion['id'])['decision'] == 'accepted'


@pytest.mark.asyncio
@pytest.mark.parametrize('shared', [False, True])
async def test_accept_all_history_does_not_create_person_edit_feedback(integration_client, doc, _registrable_roots, shared):
    doc.write_text(BODY)
    if shared:
        category = doc.parent / 'shared' / f'planning-{doc.stem}'
        category.mkdir(parents=True)
        (category / 'INDEX.md').write_text('# Planning\n')
        path = category / doc.name
        owner = _mk_thread(integration_client, _OWNER, title='Shared owner')
        from infra.database import get_db
        db = get_db()
        try:
            db.execute('UPDATE threads SET agent_enabled=1 WHERE id=?', (owner,))
            db.commit()
        finally:
            db.close()
        from registry.durable_writes import write_text
        created = await asyncio.to_thread(
            write_text, path, BODY, thread_id=owner, project_id=None, workspace=doc.parent,
            description='Planning document', update_trigger='When the plan changes', notify=False,
        )
        assert created['artifact']['path'] == str(path.resolve())
        artifact = created['artifact']['id']
        doc = path
    else:
        _, artifact = await _own(integration_client, doc)
    helper = _mk_thread(integration_client, _OWNER, title='Helper')
    suggestion = await _suggest(helper, doc, replacement='# New design', quote=HEADING)
    await asyncio.to_thread(_raise_ask, artifact)
    accepted = _accept_all(integration_client, artifact)
    assert accepted.status_code == 200, accepted.text
    feedback_before = _review(integration_client, artifact)['round_feedback_counts']['edits']
    after = doc.read_text()
    body = {'event_id': accepted.json()['decision_event_id'], 'ids': [suggestion['id']]}
    url = f'/api/artifacts/{artifact}/suggestions/history'
    undo = integration_client.post(url, params={'token': _OWNER}, json={
        **body, 'direction': 'undo', 'expected_sha256': digest(after), 'text': BODY,
    })
    assert undo.status_code == 200, undo.text
    assert _review(integration_client, artifact)['round_feedback_counts']['edits'] == feedback_before
    redo = integration_client.post(url, params={'token': _OWNER}, json={
        **body, 'direction': 'redo', 'expected_sha256': digest(BODY), 'text': after,
    })
    assert redo.status_code == 200, redo.text
    assert _review(integration_client, artifact)['round_feedback_counts']['edits'] == feedback_before
    assert _clear(integration_client, artifact, verdict='approved').status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize('new_decision', ['reject', 'accept'])
async def test_redo_refuses_a_suggestion_decided_again_after_undo(integration_client, doc, _registrable_roots, new_decision):
    owner, path = await _own_in_folder(integration_client, doc)
    before = path.read_text()
    suggestion = await _suggest(owner, path)
    accepted = _user_decide(integration_client, suggestion['id'], 'accept').json()['comment']
    after = path.read_text()
    url = f"/api/artifacts/{accepted['artifact_id']}/suggestions/history"
    body = {'event_id': accepted['decision_event_id'], 'ids': [suggestion['id']]}
    undo = integration_client.post(url, params={'token': _OWNER}, json={
        **body, 'direction': 'undo', 'expected_sha256': digest(after), 'text': before,
    })
    assert undo.status_code == 200, undo.text
    decided = _user_decide(integration_client, suggestion['id'], new_decision)
    assert decided.status_code == 200, decided.text
    redo = integration_client.post(url, params={'token': _OWNER}, json={
        **body, 'direction': 'redo', 'expected_sha256': digest(before), 'text': after,
    })
    assert redo.status_code == 409
    assert path.read_text() == (after if new_decision == 'accept' else before)
    assert _row(suggestion['id'])['decision'] == ('accepted' if new_decision == 'accept' else 'rejected')


@pytest.mark.asyncio
async def test_failed_undo_preserves_outside_edit_before_restore(integration_client, doc, _registrable_roots, monkeypatch):
    import sqlite3
    from doceditor import suggestion_history_actions

    owner, path = await _own_in_folder(integration_client, doc)
    before = path.read_text()
    suggestion = await _suggest(owner, path)
    accepted = _user_decide(integration_client, suggestion['id'], 'accept').json()['comment']
    after = path.read_text()
    outside = before + '\nOutside edit\n'

    def fail_after_outside_edit(*args, **kwargs):
        path.write_text(outside)
        raise sqlite3.OperationalError('injected decision failure')

    monkeypatch.setattr(suggestion_history_actions, '_record', fail_after_outside_edit)
    response = integration_client.post(
        f"/api/artifacts/{accepted['artifact_id']}/suggestions/history", params={'token': _OWNER}, json={
            'event_id': accepted['decision_event_id'], 'ids': [suggestion['id']], 'direction': 'undo',
            'expected_sha256': digest(after), 'text': before,
        },
    )
    assert response.status_code >= 400
    assert path.read_text() == outside
    assert _row(suggestion['id'])['decision'] == 'accepted'
    assert receipt(accepted['decision_event_id'])['state'] == 'accepted'


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason='Accept recording failure can leave applied text; see #3099')
async def test_failed_accept_receipt_restores_text_and_allows_retry(integration_client, doc, _registrable_roots, monkeypatch):
    import sqlite3
    from doceditor import suggestion_history

    owner, path = await _own_in_folder(integration_client, doc)
    before = path.read_text()
    suggestion = await _suggest(owner, path)
    called = []

    def fail_receipt(*args, **kwargs):
        called.append(True)
        raise sqlite3.OperationalError('injected receipt failure')

    with monkeypatch.context() as fault:
        fault.setattr(suggestion_history, 'record_history', fail_receipt)
        failed = _user_decide(integration_client, suggestion['id'], 'accept')
    assert called and failed.status_code >= 400
    assert path.read_text() == before
    assert _row(suggestion['id'])['decision'] is None
    retry = _user_decide(integration_client, suggestion['id'], 'accept')
    assert retry.status_code == 200, retry.text
    assert path.read_text() != before
    assert _row(suggestion['id'])['decision'] == 'accepted'


@pytest.mark.asyncio
async def test_batch_decisions_join_the_callers_transaction(integration_client, doc, _registrable_roots):
    from doceditor.comment_batch_actions import _record_batch
    from infra.database import get_db, write_transaction

    doc.write_text(BODY)
    owner, _ = await _own(integration_client, doc)
    first = await _suggest(owner, doc, replacement='# New design', quote=HEADING)
    second = await _suggest(owner, doc, replacement='A concise tail sentence', quote=TAIL)
    def check():
        db = get_db()
        try:
            with pytest.raises(RuntimeError, match='roll back owner'):
                with write_transaction(db, scope='test_batch_owner'):
                    _record_batch([first['id'], second['id']], 'accept', decided_by='user',
                                  actor_thread_id=None, reason=None, db=db)
                    assert db.execute('SELECT decision FROM doc_comments WHERE id=?', (first['id'],)).fetchone()[0] == 'accepted'
                    raise RuntimeError('roll back owner')
            assert _row(first['id'])['decision'] is None
            assert _row(second['id'])['decision'] is None
        finally:
            db.close()
    await asyncio.to_thread(check)


@pytest.mark.asyncio
async def test_transition_record_joins_the_callers_transaction(integration_client, doc, _registrable_roots):
    from doceditor.suggestion_history_actions import _record
    from infra.database import get_db, write_transaction

    owner, path = await _own_in_folder(integration_client, doc)
    suggestion = await _suggest(owner, path)
    accepted = _user_decide(integration_client, suggestion['id'], 'accept').json()['comment']
    event = receipt(accepted['decision_event_id'])
    def check():
        db = get_db()
        try:
            with pytest.raises(RuntimeError, match='roll back owner'):
                with write_transaction(db, scope='test_transition_owner'):
                    _record(event, [suggestion['id']], 'undo', db=db)
                    assert db.execute('SELECT state FROM doc_suggestion_history WHERE event_id=?',
                                      (event['event_id'],)).fetchone()[0] == 'undone'
                    raise RuntimeError('roll back owner')
            assert db.execute('SELECT state FROM doc_suggestion_history WHERE event_id=?',
                              (event['event_id'],)).fetchone()[0] == 'accepted'
            assert _row(suggestion['id'])['decision'] == 'accepted'
        finally:
            db.close()
    await asyncio.to_thread(check)


@pytest.mark.asyncio
async def test_registration_and_history_transition_roll_back_together(integration_client, doc, _registrable_roots):
    from doceditor.suggestion_history_actions import _record
    from infra.database import get_db, write_transaction
    from registry import models, service

    owner, path = await _own_in_folder(integration_client, doc)
    suggestion = await _suggest(owner, path)
    accepted = _user_decide(integration_client, suggestion['id'], 'accept').json()['comment']
    event = receipt(accepted['decision_event_id'])

    def check():
        db = get_db()
        try:
            original = models.get_artifact(accepted['artifact_id'], db=db)['metadata']
            with pytest.raises(RuntimeError, match='roll back publication'):
                with write_transaction(db, scope='test_publication_owner'):
                    service.register_write_strict(owner, str(path), metadata={'history_probe': 'pending'}, db=db)
                    _record(event, [suggestion['id']], 'undo', db=db)
                    assert 'history_probe' in models.get_artifact(accepted['artifact_id'], db=db)['metadata']
                    assert db.execute('SELECT state FROM doc_suggestion_history WHERE event_id=?',
                                      (event['event_id'],)).fetchone()[0] == 'undone'
                    raise RuntimeError('roll back publication')
            assert models.get_artifact(accepted['artifact_id'], db=db)['metadata'] == original
            assert db.execute('SELECT state FROM doc_suggestion_history WHERE event_id=?',
                              (event['event_id'],)).fetchone()[0] == 'accepted'
            assert _row(suggestion['id'])['decision'] == 'accepted'
        finally:
            db.close()
    await asyncio.to_thread(check)


@pytest.mark.asyncio
async def test_durable_registration_and_transition_share_owner_rollback(integration_client, doc, _registrable_roots):
    from doceditor.suggestion_history_actions import _record
    from infra.database import get_db, write_transaction
    from registry import durable_writes, models
    from registry.shared_target import SharedTarget

    owner, path = await _own_in_folder(integration_client, doc)
    suggestion = await _suggest(owner, path)
    accepted = _user_decide(integration_client, suggestion['id'], 'accept').json()['comment']
    event = receipt(accepted['decision_event_id'])

    def check():
        db = get_db()
        try:
            artifact = models.get_artifact(accepted['artifact_id'], db=db)
            original = artifact['metadata']
            target = SharedTarget(path, path.parent, path.parent / 'INDEX.md', False)
            before_bytes = path.read_bytes()
            with pytest.raises(RuntimeError, match='roll back publication'):
                with write_transaction(db, scope='test_durable_publication_owner'):
                    durable_writes._register_applied(
                        target, None, thread_id=owner, description=None,
                        external_id=artifact['external_id'], content_hash='test-publication-digest',
                        before=before_bytes, db=db,
                    )
                    _record(event, [suggestion['id']], 'undo', db=db)
                    assert 'test-publication-digest' in models.get_artifact(accepted['artifact_id'], db=db)['metadata']
                    assert db.execute('SELECT state FROM doc_suggestion_history WHERE event_id=?',
                                      (event['event_id'],)).fetchone()[0] == 'undone'
                    raise RuntimeError('roll back publication')
            assert models.get_artifact(accepted['artifact_id'], db=db)['metadata'] == original
            assert db.execute('SELECT state FROM doc_suggestion_history WHERE event_id=?',
                              (event['event_id'],)).fetchone()[0] == 'accepted'
            assert _row(suggestion['id'])['decision'] == 'accepted'
        finally:
            db.close()
    await asyncio.to_thread(check)


@pytest.mark.asyncio
@pytest.mark.parametrize('outside_edit', [False, True])
async def test_injected_stop_after_registration_retries_in_fresh_service(
    integration_client, doc, _registrable_roots, monkeypatch, outside_edit,
):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    from api.doc_comments import _person_writer
    from doceditor import suggestion_history_actions
    from infra.auth import authenticate
    from infra.database import get_db
    from registry.durable_writes import recover_write_intents

    owner, path = await _own_in_folder(integration_client, doc)
    before = path.read_text()
    suggestion = await _suggest(owner, path)
    accepted = _user_decide(integration_client, suggestion['id'], 'accept').json()['comment']
    after = path.read_text()
    body = {'event_id': accepted['decision_event_id'], 'ids': [suggestion['id']], 'direction': 'undo',
            'expected_sha256': digest(after), 'text': before}
    user = authenticate(_OWNER)

    class InjectedStop(BaseException):
        pass

    def interrupt(*args, **kwargs):
        raise InjectedStop()

    with monkeypatch.context() as fault:
        fault.setattr(suggestion_history_actions, '_record', interrupt)
        with pytest.raises(InjectedStop):
            await suggestion_history_actions.transition_suggestion_history(
                accepted['artifact_id'], **body, user_id=user['id'], write=_person_writer(user, {}),
            )
    assert path.read_text() == before
    assert _row(suggestion['id'])['decision'] == 'accepted'
    await asyncio.to_thread(recover_write_intents)
    if outside_edit:
        path.write_text('a later outside edit')
    db = get_db()
    try:
        db_path = db.execute('PRAGMA database_list').fetchone()[2]
    finally:
        db.close()
    script = '''
import asyncio, json, sys
from pathlib import Path
from infra import config, database
config.DB_PATH = database.DB_PATH = Path(sys.argv[1])
from doceditor.comment_action_errors import CommentActionError
from doceditor.suggestion_history_actions import transition_suggestion_history
from registry import service
service.register_write_strict = lambda *a, **kw: (_ for _ in ()).throw(AssertionError('registered twice'))
async def no_write(*a, **kw):
    raise AssertionError('wrote twice')
request = json.load(sys.stdin)
try:
    result = asyncio.run(transition_suggestion_history(**request, write=no_write))
    print(json.dumps({'result': result}))
except CommentActionError as exc:
    print(json.dumps({'error': exc.kind}))
'''
    backend = str(Path(__file__).resolve().parents[1])
    env = {**os.environ, 'PYTHONPATH': backend}
    request = {'artifact_id': accepted['artifact_id'], **body, 'user_id': user['id']}
    completed = await asyncio.to_thread(
        subprocess.run, [sys.executable, '-c', script, db_path], input=json.dumps(request),
        text=True, capture_output=True, env=env, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    if outside_edit:
        assert result == {'error': 'stale'}
        assert path.read_text() == 'a later outside edit'
        assert _row(suggestion['id'])['decision'] == 'accepted'
    else:
        assert result['result']['revision'] == digest(before)
        assert path.read_text() == before
        assert _row(suggestion['id'])['decision'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('direction', ['undo', 'redo'])
async def test_failed_decision_record_restores_text_and_allows_retry(
    integration_client, doc, _registrable_roots, monkeypatch, direction,
):
    import sqlite3
    from doceditor import suggestion_history_actions

    owner, path = await _own_in_folder(integration_client, doc)
    before = path.read_text()
    suggestion = await _suggest(owner, path)
    accepted = _user_decide(integration_client, suggestion['id'], 'accept').json()['comment']
    after = path.read_text()
    called = []
    def fail_record(*args, **kwargs):
        called.append(True)
        raise sqlite3.OperationalError('injected record failure')
    url = f"/api/artifacts/{accepted['artifact_id']}/suggestions/history"
    body = {'event_id': accepted['decision_event_id'], 'ids': [suggestion['id']], 'direction': 'undo',
            'expected_sha256': digest(after), 'text': before}
    if direction == 'redo':
        undone = integration_client.post(url, params={'token': _OWNER}, json=body)
        assert undone.status_code == 200, undone.text
        body = {**body, 'direction': 'redo', 'expected_sha256': digest(before), 'text': after}
    original, target = (after, before) if direction == 'undo' else (before, after)
    original_decision, target_decision = ('accepted', None) if direction == 'undo' else (None, 'accepted')
    with monkeypatch.context() as fault:
        fault.setattr(suggestion_history_actions, '_record', fail_record)
        failed = integration_client.post(url, params={'token': _OWNER}, json=body)
    assert called and failed.status_code >= 400
    # The person is told the document is unchanged and what to do next; the reload that follows drops the step, so no retry.
    assert failed.json()['detail'] == suggestion_history_actions._not_recorded(direction)
    assert failed.json()['detail'].startswith(f'Couldn’t {direction} that. The document is unchanged.')
    assert path.read_text() == original
    assert _row(suggestion['id'])['decision'] == original_decision
    retry = integration_client.post(url, params={'token': _OWNER}, json=body)
    assert retry.status_code == 200, retry.text
    assert path.read_text() == target
    assert _row(suggestion['id'])['decision'] == target_decision
