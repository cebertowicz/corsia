"""Account views and their read-lane job count."""
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from api import vendor_accounts as api
from connections import harness_usage
from connections.account_evidence import mark_discovery
from connections.corsia_agent_account import account_state
from connections.vendor_accounts import get_account
from infra.auth import require_user
from vendor_account_fixtures import accounts, receipt_of  # noqa: F401


STAMP = '2026-01-01T00:00:00+00:00'


def _discover(db, account, models):
    receipt = receipt_of('codex', account['id'], db=db)
    assert mark_discovery(account['id'], receipt=receipt, status='ready', models=models, db=db)
    db.commit()


def _make(db, account, state):
    if state == 'unchecked':
        return
    _discover(db, account, {'codex/test': {'available': state != 'no_usable_model'}})
    if state == 'limited':
        harness_usage.record_limited(provider='codex', receipt=receipt_of('codex', account['id'], db=db),
                                     reason='usage_limit', resets_at='2099-01-01T00:00:00+00:00',
                                     source='rate_limit_update', db=db)


def _expected(account_id, name, *, default=False, state='unchecked', revision=1):
    limited = state == 'limited'
    usage = {'allow_overage': False, 'check': {'at': STAMP, 'error': None, 'outcome': 'limited'} if limited else None,
             'checking': False, 'facts': {}, 'observed_at': STAMP if limited else None,
             'reason': 'usage_limit' if limited else None,
             'resets_at': '2099-01-01T00:00:00+00:00' if limited else None,
             'state': 'limited' if limited else 'unknown', 'usable': not limited, 'windows': []}
    label = 'ChatGPT' if default else name
    return {'id': account_id, 'provider': 'codex', 'name': name, 'identity': {}, 'state': 'signed_in',
            'is_default': int(default), 'revision': revision, 'upgrade_reason': None, 'duplicate_of': None,
            'allow_overage': False, 'no_usable_model': state == 'no_usable_model',
            'label': {'custom': None if default else name, 'email': None, 'identified': not default,
                      'name': label, 'text': label},
            'billing': None, 'billing_revision': 0, 'usage': usage}


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_user] = lambda: {'id': 'user'}
    with TestClient(app, raise_server_exceptions=False) as result:
        yield result


@pytest.mark.parametrize('state', ['ready', 'limited', 'no_usable_model', 'unchecked'])
def test_list_responses_across_states(accounts, client, monkeypatch, state):
    monkeypatch.setattr(harness_usage, '_now', lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    db, default, work, personal = accounts
    _make(db, work, state)
    assert account_state(get_account(work['id'], db=db), project_ids=[None], db=db)[0] == state
    expected = {'accounts': [_expected(default['id'], 'Default', default=True),
                             _expected(work['id'], 'Second', state=state),
                             _expected(personal['id'], 'Third')]}
    for params in ({}, {'provider': 'codex'}, {'provider': 'codex', 'include_removed': 'true'}):
        result = client.get('/api/vendor-accounts', params=params)
        assert result.status_code == 200
        assert result.json() == expected
    assert client.get('/api/vendor-accounts', params={'provider': 'claude-code'}).json() == {'accounts': []}


def test_later_ready_model_and_invalid_provider(accounts, client):
    db, default, work, personal = accounts
    other = 'codex/z-second'
    db.execute('INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)',
               (other, other, other, 'codex', 'z-second', 'codex', 't', 't'))
    db.commit()
    _discover(db, work, {'codex/test': {'available': False}, other: {'available': True}})
    assert account_state(get_account(work['id'], db=db), project_ids=[None], db=db)[0] == 'ready'
    assert client.get('/api/vendor-accounts', params={'provider': 'codex'}).json() == {
        'accounts': [_expected(default['id'], 'Default', default=True), _expected(work['id'], 'Second'),
                     _expected(personal['id'], 'Third')]}
    invalid = client.get('/api/vendor-accounts', params={'provider': 'invalid-provider'})
    assert invalid.status_code == 409
    assert invalid.json() == {'detail': 'account provider is not registered'}


def test_single_account_response(accounts, client):
    _, _, work, _ = accounts
    response = client.patch(f"/api/vendor-accounts/{work['id']}",
                            json={'name': 'Renamed', 'expected_revision': work['revision']})
    assert response.status_code == 200
    assert response.json() == _expected(work['id'], 'Renamed', revision=2)


def test_list_and_single_view_each_use_one_read_job(accounts, client, monkeypatch):
    calls = []
    original = api.run_read

    async def count(fn, *args, **kwargs):
        calls.append(fn)
        return await original(fn, *args, **kwargs)

    monkeypatch.setattr(api, 'run_read', count)
    assert client.get('/api/vendor-accounts').status_code == 200
    assert len(calls) == 1
    calls.clear()
    _, _, work, _ = accounts
    response = client.patch(f"/api/vendor-accounts/{work['id']}",
                            json={'name': 'Renamed', 'expected_revision': work['revision']})
    assert response.status_code == 200
    # Other read jobs prepare the view; the composition itself is one job.
    assert sum(fn is api._account_view for fn in calls) == 1


def test_account_conflict_is_translated_in_list_and_single_view(accounts, client, monkeypatch):
    def fail(*_args, **_kwargs):
        raise api.accounts.AccountError('duplicate lookup failed')
    monkeypatch.setattr(api.accounts, 'duplicate_of', fail)
    _, _, work, _ = accounts
    results = [client.get('/api/vendor-accounts'),
               client.patch(f"/api/vendor-accounts/{work['id']}",
                            json={'name': 'Renamed', 'expected_revision': work['revision']})]
    for result in results:
        assert result.status_code == 409
        assert result.json() == {'detail': 'duplicate lookup failed'}


def test_unexpected_usage_failure_is_not_translated(accounts, client, monkeypatch):
    def fail(_account):
        raise api.accounts.AccountError('usage failed')
    monkeypatch.setattr(api, '_allow_overage', fail)
    _, _, work, _ = accounts
    assert client.get('/api/vendor-accounts').status_code == 500
    assert client.patch(f"/api/vendor-accounts/{work['id']}",
                        json={'name': 'Renamed', 'expected_revision': work['revision']}).status_code == 500
