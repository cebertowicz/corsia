"""Added-account discovery must populate the shared catalog on a clean install."""
from types import SimpleNamespace

import pytest

from vendor_account_fixtures import accounts  # noqa: F401


@pytest.mark.parametrize('provider', ['codex', 'claude-code'])
def test_discovery_populates_catalog_and_connection(accounts, provider):
    from connections.account_discovery_commit import commit_discovery_bundle
    from connections.account_receipts import capture_receipt
    from connections.models import connection_available, upsert_connection
    from connections.vendor_accounts import add_account, record_default_identity, set_account_state
    from llm.model_status import _harness_status, scoped_status_rows

    db = accounts[0]
    upsert_connection(type='cli', provider=provider, status='available', config={'binary': '/bin/test'})
    db.execute("UPDATE connections SET status='needs_auth' WHERE provider=?", (provider,))
    db.commit()
    record_default_identity(provider, {}, db=db)
    if provider == 'codex':
        account = accounts[2]
    else:
        account = add_account(provider, 'Added')
        account = set_account_state(account['id'], 'signed_in', expected_revision=0)
    receipt = capture_receipt(provider, account_id=account['id'], db=db)
    row = {'selector_id': 'new-model', 'display_name': 'New model', 'available': True,
           'resolved_identity': 'new-model-1', 'context_window': 200000}
    staged = SimpleNamespace(usage_report=None, cache_stage=None)
    result = commit_discovery_bundle(provider, staged, [row],
        context=SimpleNamespace(receipt=receipt, account_id=account['id']), harness_version='1', db=db)
    assert result.status == 'ready'
    stored = db.execute('SELECT * FROM models WHERE id=?', (provider + '/new-model',)).fetchone()
    assert stored is not None
    assert connection_available(provider, db=db)
    assert _harness_status(provider, dict(stored), {}, stored['context_window'], True) == ('ready', None)
    projected = next(item for item in scoped_status_rows(account_id=account['id']) if item['id'] == provider + '/new-model')
    runs = {entry['account_id']: entry for entry in projected['runs_on']}
    assert runs[account['id']]['status'] == 'ready'
    from connections.vendor_accounts import default_account
    assert runs[default_account(provider, db=db)['id']]['status'] == 'unavailable'


def test_missing_binary_and_undiscovered_account_remain_unavailable(accounts, monkeypatch):
    from connections.account_evidence import mark_discovery
    from connections.account_receipts import capture_receipt
    from connections.models import connection_available

    db, _, account, _ = accounts
    db.execute("UPDATE connections SET status='needs_auth' WHERE provider='codex'")
    db.commit()
    assert not connection_available('codex', db=db)
    receipt = capture_receipt('codex', account_id=account['id'], db=db)
    assert mark_discovery(account['id'], receipt=receipt, status='ready', models={'codex/test': {}}, db=db)
    db.commit()
    monkeypatch.setattr('connections.resolve_builtin_binary', lambda provider, *, db=None: ({}, None))
    assert not connection_available('codex', db=db)
    db.execute("UPDATE connections SET status='not_installed' WHERE provider='codex'")
    db.commit()
    assert not connection_available('codex', db=db)


def test_discovery_does_not_retire_other_account_or_default_rows(accounts):
    from connections.account_discovery_commit import commit_discovery_bundle
    from connections.account_receipts import capture_receipt
    from connections.harness_models import store_success

    db, _, account, _ = accounts
    row = lambda selector: {'selector_id': selector, 'available': True, 'resolved_identity': selector,
                            'context_window': 200000}
    store_success('codex', [row('default-only')], harness_version='1', source='codex_cli', timestamp='t', db=db)
    receipt = capture_receipt('codex', account_id=account['id'], db=db)
    staged = SimpleNamespace(usage_report=None, cache_stage=None)
    commit_discovery_bundle('codex', staged, [row('added-only')],
        context=SimpleNamespace(receipt=receipt, account_id=account['id']), harness_version='1', db=db)
    assert db.execute("SELECT available FROM models WHERE id='codex/default-only'").fetchone()[0] == 1
    store_success('codex', [row('default-only')], harness_version='1', source='codex_cli', timestamp='t2', db=db)
    assert db.execute("SELECT available FROM models WHERE id='codex/added-only'").fetchone()[0] == 1
    unavailable = row('default-only') | {'available': False}
    commit_discovery_bundle('codex', staged, [unavailable],
        context=SimpleNamespace(receipt=receipt, account_id=account['id']), harness_version='1', db=db)
    assert db.execute("SELECT available FROM models WHERE id='codex/default-only'").fetchone()[0] == 1


def test_merge_keeps_exact_context_when_identity_is_not_reported(accounts):
    import json
    from connections.harness_models import store_success

    db = accounts[0]
    row = {'selector_id': 'context-test', 'available': True, 'resolved_identity': 'exact-model',
           'context_window': 200000, 'context_observation': {'effective_tokens': 200000, 'source': 'runtime'}}
    store_success('codex', [row], harness_version='1', source='codex_cli', timestamp='t', db=db)
    store_success('codex', [{'selector_id': 'context-test', 'available': True}],
                  harness_version='1', source='codex_cli', timestamp='t2', merge=True, db=db)
    stored = db.execute("SELECT context_window, metadata_json FROM models WHERE id='codex/context-test'").fetchone()
    assert stored['context_window'] == 200000
    assert json.loads(stored['metadata_json'])['context_observation'] == row['context_observation']


def test_superseded_discovery_never_writes_catalog(accounts):
    from connections.account_discovery_commit import commit_discovery_bundle
    from connections.account_receipts import capture_receipt
    from connections.vendor_accounts import set_account_state

    db, _, account, _ = accounts
    receipt = capture_receipt('codex', account_id=account['id'], db=db)
    set_account_state(account['id'], 'signed_out', expected_revision=account['revision'])
    result = commit_discovery_bundle('codex', SimpleNamespace(usage_report=None, cache_stage=None),
        [{'selector_id': 'stale', 'available': True}], context=SimpleNamespace(receipt=receipt, account_id=account['id']),
        harness_version='1', db=db)
    assert result.cause == 'account_refresh_superseded'
    assert db.execute("SELECT id FROM models WHERE id='codex/stale'").fetchone() is None
