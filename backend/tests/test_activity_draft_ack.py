"""Activity acknowledgement persists and is scoped to the owned sending draft."""
import sqlite3

import httpx
import pytest
from fastapi import FastAPI

from api.attention import router, _acknowledge_draft
from infra.activity_draft_rows import draft_rows
from infra.auth import require_user
from infra.database import get_db
from infra.migration_211_to_212_activity_draft_ack import migrate_211_to_212
from infra.schema import SCHEMA_SQL
from tests.test_email_drafts_api import _artifact, _owner
from tests.test_google_email import _draft, client_file, conductor, google, mail  # noqa: F401
from tests.test_google_sign_in import _REAL_CLIENT


def test_schema_on_upgrade_and_new_install():
    from infra.database_migrations import MIGRATIONS

    upgrade = sqlite3.connect(':memory:')
    upgrade.execute('CREATE TABLE action_drafts (id TEXT PRIMARY KEY, state TEXT NOT NULL)')
    upgrade.execute("INSERT INTO action_drafts VALUES ('existing', 'sending')")
    assert MIGRATIONS[211] is migrate_211_to_212
    migrate_211_to_212(upgrade)
    migrate_211_to_212(upgrade)
    assert upgrade.execute('SELECT * FROM action_drafts').fetchall() == [('existing', 'sending')]
    upgrade.execute("INSERT INTO activity_draft_acknowledgements VALUES ('person', 'existing')")
    fresh = sqlite3.connect(':memory:')
    fresh.executescript(SCHEMA_SQL)
    for db in (upgrade, fresh):
        assert [r[1] for r in db.execute('PRAGMA table_info(activity_draft_acknowledgements)')] == ['user_id', 'draft_id']


@pytest.mark.asyncio
async def test_acknowledgement_lifecycle_and_authorization(mail):
    saved = await _draft(mail)
    _artifact(saved)
    draft_id = saved['draft_id']
    owner = _owner(mail.thread)['id']
    db = get_db()
    assert _acknowledge_draft(draft_id, 'another-user') == 404
    assert _acknowledge_draft('missing', owner) == 404
    assert _acknowledge_draft(draft_id, owner) == 409
    db.execute("UPDATE action_drafts SET state='sending' WHERE id=?", (draft_id,))
    db.commit()
    assert _acknowledge_draft(draft_id, owner) == 204
    assert _acknowledge_draft(draft_id, owner) == 204
    # Read via a separate connection to prove the write committed.
    fresh = sqlite3.connect(db.execute('PRAGMA database_list').fetchone()[2])
    try:
        assert fresh.execute('SELECT user_id, draft_id FROM activity_draft_acknowledgements').fetchall() == [(owner, draft_id)]
    finally:
        fresh.close()
    for subjects in (True, False):
        assert not draft_rows(db, owner, subjects=subjects)
    db.execute("UPDATE action_drafts SET state='open' WHERE id=?", (draft_id,))
    db.commit()
    assert [r['external_id'] for r in draft_rows(db, owner)] == [draft_id]
    assert _acknowledge_draft(draft_id, owner) == 409
    db.execute("UPDATE action_drafts SET state='sent' WHERE id=?", (draft_id,))
    db.commit()
    assert not draft_rows(db, owner)


@pytest.mark.asyncio
async def test_endpoint_requires_authentication_and_checks_ownership(mail, monkeypatch):
    from infra import auth
    monkeypatch.setattr(auth, 'authenticate', lambda token: None)
    saved = await _draft(mail)
    owner = _owner(mail.thread)['id']
    db = get_db()
    db.execute("UPDATE action_drafts SET state='sending' WHERE id=?", (saved['draft_id'],))
    db.commit()
    app = FastAPI()
    app.include_router(router)
    url = f"/api/attention/drafts/{saved['draft_id']}/acknowledge"
    async with _REAL_CLIENT(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.post(url)).status_code == 401
        app.dependency_overrides[require_user] = lambda: {'id': 'another-user'}
        assert (await client.post(url)).status_code == 404
        app.dependency_overrides[require_user] = lambda: {'id': owner}
        response = await client.post(url)
        assert response.status_code == 204 and not response.content


@pytest.mark.asyncio
async def test_failed_write_does_not_hide_draft(mail):
    saved = await _draft(mail)
    _artifact(saved)
    owner = _owner(mail.thread)['id']
    db = get_db()
    db.execute("UPDATE action_drafts SET state='sending' WHERE id=?", (saved['draft_id'],))
    db.execute("CREATE TRIGGER reject_ack BEFORE INSERT ON activity_draft_acknowledgements BEGIN SELECT RAISE(ABORT, 'failure'); END")
    db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        _acknowledge_draft(saved['draft_id'], owner)
    assert [r['external_id'] for r in draft_rows(db, owner)] == [saved['draft_id']]
