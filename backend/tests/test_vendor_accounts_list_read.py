"""The account list preserves its response while combining read-lane jobs."""
import asyncio

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

from api import vendor_accounts as api
from connections import harness_usage
from connections.account_evidence import mark_discovery
from connections.corsia_agent_account import account_state
from connections.vendor_accounts import default_account, get_account, set_account_state
from infra.auth import require_user
from vendor_account_fixtures import accounts, receipt_of  # noqa: F401


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


async def _prior_response(provider=None, include_removed=False):
    """The former composition pins every field and JSON byte."""
    rows = await api._read(api.accounts.list_accounts, provider=provider, include_removed=include_removed)
    numbered = rows if include_removed else await api._read(
        api.accounts.list_accounts, provider=provider, include_removed=True)
    billing = await api.run_read(api._billing_views)
    no_model = await api._no_usable_model((row['provider'] for row in rows), rows)
    return {'accounts': [await api._shown(row, rows, numbered, billing, no_model) for row in rows]}


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_user] = lambda: {'id': 'user'}
    with TestClient(app, raise_server_exceptions=False) as result:
        yield result


@pytest.mark.parametrize('first,second', [
    ('no_usable_model', 'no_usable_model'), ('limited', 'limited'),
    ('ready', 'limited'), ('unchecked', 'no_usable_model'),
])
def test_list_bytes_across_states(accounts, client, first, second):
    db, _, work, personal = accounts
    _make(db, work, first)
    _make(db, personal, second)
    if first != 'ready' and second != 'ready':
        default = default_account('codex', db=db)
        set_account_state(default['id'], 'signed_out', expected_revision=default['revision'], db=db)
    assert account_state(get_account(work['id'], db=db), project_ids=[None], db=db)[0] == first
    assert account_state(get_account(personal['id'], db=db), project_ids=[None], db=db)[0] == second
    for provider, include_removed in [(None, False), ('codex', False), ('codex', True), ('claude-code', False)]:
        expected = JSONResponse(asyncio.run(_prior_response(provider, include_removed))).body
        result = client.get('/api/vendor-accounts', params={
            **({'provider': provider} if provider else {}), 'include_removed': str(include_removed).lower(),
        })
        assert result.status_code == 200
        assert result.content == expected


def test_later_ready_model_and_invalid_provider(accounts, client):
    db, _, work, _ = accounts
    other = 'codex/z-second'
    db.execute('INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)',
               (other, other, other, 'codex', 'z-second', 'codex', 't', 't'))
    db.commit()
    _discover(db, work, {'codex/test': {'available': False}, other: {'available': True}})
    assert account_state(get_account(work['id'], db=db), project_ids=[None], db=db)[0] == 'ready'
    expected = JSONResponse(asyncio.run(_prior_response('codex'))).body
    assert client.get('/api/vendor-accounts', params={'provider': 'codex'}).content == expected
    invalid = client.get('/api/vendor-accounts', params={'provider': 'invalid-provider'})
    assert invalid.status_code == 409
    assert invalid.json() == {'detail': 'account provider is not registered'}


def test_unexpected_usage_failure_is_not_translated(accounts, client, monkeypatch):
    def fail(_account):
        raise api.accounts.AccountError('usage failed')
    monkeypatch.setattr(api, '_allow_overage', fail)
    assert client.get('/api/vendor-accounts').status_code == 500
