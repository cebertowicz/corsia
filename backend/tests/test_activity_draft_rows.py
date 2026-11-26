"""Open email drafts appear in the Activity queue from their actual documents."""
from __future__ import annotations

import asyncio

import pytest

from infra.database import get_db
from infra.activity_draft_rows import draft_rows
from tests.test_email_drafts_api import _artifact, _owner
from tests.test_google_email import (  # noqa: F401 (fixtures)
    _draft, client_file, conductor, google, mail,
)


@pytest.mark.asyncio
async def test_open_draft_follows_document_and_owner(mail):
    saved = await _draft(mail)
    artifact_id = _artifact(saved)
    owner = _owner(mail.thread)['id']
    db = get_db()
    try:
        rows = draft_rows(db, owner)
        row = next(row for row in rows if row['external_id'] == saved['draft_id'])
        assert row['open']['artifact_id'] == artifact_id
        assert row['title'].startswith('Send: ')
        assert not draft_rows(db, 'another-user')
        # A send claim is not a settled draft: retain the row until its outcome is known.
        db.execute("UPDATE action_drafts SET state='sending' WHERE id=?", (saved['draft_id'],))
        db.commit()
        assert any(row['external_id'] == saved['draft_id'] for row in draft_rows(db, owner))
        assert any(row['external_id'] == saved['draft_id'] for row in draft_rows(db, owner, subjects=False))
        db.execute("UPDATE action_drafts SET state='discarded' WHERE id=?", (saved['draft_id'],))
        db.commit()
        assert not any(row['external_id'] == saved['draft_id'] for row in draft_rows(db, owner))
    finally:
        db.close()


@pytest.mark.asyncio
async def test_subject_comes_from_live_file_and_sweep_does_not_read_it(mail, monkeypatch):
    from infra import activity_draft_rows

    saved = await _draft(mail)
    artifact_id = _artifact(saved)
    owner = _owner(mail.thread)['id']
    path = mail.workspace / saved['path']
    path.write_text(path.read_text().replace('Subject: Lunch', 'Subject: Updated subject'))
    db = get_db()
    try:
        [row] = draft_rows(db, owner)
        assert row['title'] == 'Send: Updated subject'
        monkeypatch.setattr(activity_draft_rows, '_read_bytes', lambda path: (_ for _ in ()).throw(AssertionError('sweep read a file')))
        [row] = draft_rows(db, owner, subjects=False)
        assert row['open']['artifact_id'] == artifact_id
    finally:
        db.close()


@pytest.mark.asyncio
async def test_new_draft_notifies_after_commit(mail, monkeypatch):
    from infra import state

    observed = []
    def broadcast(user, frame):
        if frame['type'] == 'artifacts_changed':
            db = get_db()
            try:
                observed.append(db.execute("SELECT COUNT(*) FROM action_drafts WHERE kind='email' AND state='open'").fetchone()[0])
            finally:
                db.close()
    monkeypatch.setattr(state, 'broadcast_to_user', broadcast)
    await _draft(mail)
    assert observed == [1]


@pytest.mark.asyncio
async def test_lifecycle_notifications_follow_commit_and_failure_is_nonfatal(mail, monkeypatch):
    from infra import state
    from infra.database import write_transaction
    from tools.action_drafts import record_draft, discard_draft

    saved = await _draft(mail)
    frames = []
    monkeypatch.setattr(state, 'broadcast_to_user', lambda user, frame: frames.append(frame))
    db = get_db()
    try:
        data = (mail.workspace / saved['path']).read_bytes()
        with write_transaction(db, scope='test_outer'):
            record_draft(saved['draft_id'], data, db=db)
            assert frames == []
        assert frames == [{'type': 'artifacts_changed', 'draft_id': saved['draft_id'],
                           'thread_id': mail.thread, 'kind': 'email', 'state': 'open'}]
        frames.clear()
        with pytest.raises(RuntimeError):
            with write_transaction(db, scope='test_rollback'):
                discard_draft(saved['draft_id'], db=db)
                raise RuntimeError('rollback')
        assert frames == []
        monkeypatch.setattr(state, 'broadcast_to_user', lambda *args: (_ for _ in ()).throw(RuntimeError('offline')))
        discard_draft(saved['draft_id'], db=db)
        assert db.execute('SELECT state FROM action_drafts WHERE id=?', (saved['draft_id'],)).fetchone()[0] == 'discarded'
    finally:
        db.close()


@pytest.mark.asyncio
async def test_notification_scheduling_failure_does_not_fail_a_draft_save(mail, monkeypatch):
    from tools import action_drafts

    monkeypatch.setattr(action_drafts, 'after_commit', lambda *args: (_ for _ in ()).throw(RuntimeError('offline')))
    saved = await _draft(mail)
    db = get_db()
    try:
        assert db.execute('SELECT state FROM action_drafts WHERE id=?', (saved['draft_id'],)).fetchone()[0] == 'open'
    finally:
        db.close()


@pytest.mark.asyncio
async def test_existing_document_load_resolves_a_draft(mail):
    from api.files_load import read_document
    from api.email_drafts import draft_context

    saved = await _draft(mail)
    owner = _owner(mail.thread)
    document = read_document(str(mail.workspace / saved['path']), owner['id'], None, mail.thread)
    assert document['email_draft']['id'] == saved['draft_id']
    artifact_id = document['artifact']['id']
    context = await draft_context(artifact_id, owner)
    assert context['thread_id'] == mail.thread and context['state'] == 'open'


@pytest.mark.asyncio
async def test_missing_unregistered_draft_is_omitted_without_affecting_other_rows(mail):
    from tools.action_drafts import create_draft

    saved = await _draft(mail)
    _artifact(saved)
    missing = create_draft(mail.thread, 'email', 'missing-email.md', b'Subject: Missing\n')
    (mail.workspace / 'present-email.md').write_bytes(b'Subject: Present\n')
    present = create_draft(mail.thread, 'email', 'present-email.md', b'Subject: Present\n')
    owner = _owner(mail.thread)['id']
    db = get_db()
    try:
        for subjects in (True, False):
            keys = {row['external_id'] for row in draft_rows(db, owner, subjects=subjects)}
            assert saved['draft_id'] in keys
            assert present['draft_id'] in keys
            assert missing['draft_id'] not in keys
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unreadable_unregistered_draft_is_omitted(mail, monkeypatch):
    from infra import activity_draft_rows
    from tools.action_drafts import create_draft

    (mail.workspace / 'unreadable-email.md').write_bytes(b'Subject: Unreadable\n')
    saved = create_draft(mail.thread, 'email', 'unreadable-email.md', b'Subject: Unreadable\n')
    owner = _owner(mail.thread)['id']
    monkeypatch.setattr(activity_draft_rows, '_read_bytes', lambda path: None)
    db = get_db()
    try:
        assert not any(row['external_id'] == saved['draft_id'] for row in draft_rows(db, owner))
        # Ranking does not read content; it retains metadata-only membership.
        assert any(row['external_id'] == saved['draft_id'] for row in draft_rows(db, owner, subjects=False))
    finally:
        db.close()


@pytest.mark.asyncio
async def test_missing_registered_draft_remains_and_can_be_discarded(mail, monkeypatch):
    from api import email_drafts
    from registry import reconcile

    saved = await _draft(mail)
    owner = _owner(mail.thread)
    db = get_db()
    try:
        [row] = draft_rows(db, owner['id'])
        artifact_id = row['open']['artifact_id']
        assert artifact_id
        (mail.workspace / saved['path']).unlink()
        await asyncio.to_thread(reconcile.apply_for_thread, mail.thread)
        [row] = draft_rows(db, owner['id'])
        assert row['detail']['missing'] is True
        assert row['open'] is None
        assert row['detail']['artifact_id'] == artifact_id
        assert row['title'] == 'Send: email draft'
        assert draft_rows(db, owner['id'], subjects=False)[0]['external_id'] == saved['draft_id']

        async def tell(*args, **kwargs):
            pass
        monkeypatch.setattr(email_drafts, '_tell_agent', tell)
        result = await email_drafts.dont_send(artifact_id, owner)
        assert result['state'] == 'discarded'
        assert not draft_rows(db, owner['id'])
    finally:
        db.close()


@pytest.mark.asyncio
async def test_missing_draft_retired_for_another_reason_is_omitted(mail):
    from registry import models

    saved = await _draft(mail)
    artifact_id = _artifact(saved)
    owner = _owner(mail.thread)['id']
    (mail.workspace / saved['path']).unlink()
    await asyncio.to_thread(models.retire, artifact_id, tombstone=True, cause='promoted')
    db = get_db()
    try:
        assert not draft_rows(db, owner)
        assert not draft_rows(db, owner, subjects=False)
    finally:
        db.close()
