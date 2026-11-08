"""Thread account recovery uses the same person-facing label as Settings."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from connections.account_policy import select_account
from connections.vendor_accounts import set_account_state
from vendor_account_fixtures import accounts  # noqa: F401


def test_thread_account_label_matches_settings_after_earlier_account_removed(accounts):  # noqa: F811
    from api.vendor_accounts import router
    from infra.auth import require_user

    db, _default, second, third = accounts
    set_account_state(second['id'], 'removed', expected_revision=second['revision'], db=db)
    select_account('destination', 'codex', account_id=third['id'], db=db)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: {'id': 'user'}
    with TestClient(app) as client:
        listed = client.get('/api/vendor-accounts?provider=codex')
        bound = client.get('/api/threads/destination/vendor-account')
    assert listed.status_code == bound.status_code == 200
    settings = next(row for row in listed.json()['accounts'] if row['id'] == third['id'])
    account = next(row for row in bound.json()['accounts'] if row['id'] == third['id'])
    assert account['label']['text'] == settings['label']['text'] == 'Third'
