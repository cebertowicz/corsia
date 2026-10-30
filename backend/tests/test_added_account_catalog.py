"""Added account models are projected from current discovery, never stored as Default truth."""
from types import SimpleNamespace

import pytest

from vendor_account_fixtures import accounts, signed_in_default  # noqa: F401
from test_provider_sources import client  # noqa: F401


def _row(selector, available=True):
    return {'selector_id': selector, 'display_name': selector, 'available': available,
            'resolved_identity': selector, 'context_window': 200000}


def _discover(db, provider, account, rows):
    from connections.account_discovery_commit import commit_discovery_bundle
    from connections.account_receipts import capture_receipt

    receipt = capture_receipt(provider, account_id=account['id'], db=db)
    return commit_discovery_bundle(provider, SimpleNamespace(usage_report=None, cache_stage=None, unlisted_errors={}), rows,
        context=SimpleNamespace(receipt=receipt, account_id=account['id']), harness_version='1', db=db)


def _projected(provider, selector):
    from llm.model_status import scoped_status_rows

    return next(item for item in scoped_status_rows() if item['id'] == provider + '/' + selector)


@pytest.mark.parametrize('provider', ['codex', 'claude-code'])
def test_fresh_added_account_lists_models_and_sources_ready(accounts, client, provider):
    from connections.models import upsert_connection
    from connections.vendor_accounts import add_account, set_account_state
    from test_provider_sources import _sources

    db, _, codex, _ = accounts
    upsert_connection(type='cli', provider=provider, status='available', config={'binary': '/bin/test'})
    db.execute("UPDATE connections SET status='needs_auth' WHERE provider=?", (provider,))
    db.commit()
    account = codex if provider == 'codex' else add_account(provider, 'Added')
    if provider == 'claude-code':
        account = set_account_state(account['id'], 'signed_in', expected_revision=0)
    assert _discover(db, provider, account, [_row('added-only')]).status == 'ready'
    # Every signed-in account's discovery writes the rows it lists (design D3 item 5, Writer).
    assert db.execute('SELECT id FROM models WHERE id=?', (provider + '/added-only',)).fetchone() is not None
    item = _projected(provider, 'added-only')
    runs = {entry['account_id']: entry for entry in item['runs_on']}
    assert runs[account['id']]['status'] == 'ready'
    source = _sources(client)[provider]
    assert source['state'] == 'ready'
    assert next(line for line in source['accounts'] if line['account_id'] == account['id'])['state'] == 'ready'


def test_added_rows_read_once_per_fact_scope(accounts, monkeypatch):
    from connections import fact_memo, vendor_accounts
    from llm.harness_catalog import added_rows

    db = accounts[0]
    original = vendor_accounts.list_accounts
    calls = []

    def counted(*args, **kwargs):
        calls.append(kwargs['provider'])
        return original(*args, **kwargs)

    monkeypatch.setattr(vendor_accounts, 'list_accounts', counted)
    with fact_memo.fact_reads():
        first = added_rows(db=db)
        assert added_rows(db=db) is first
    assert calls == ['claude-code', 'codex']
    added_rows(db=db)
    assert calls == ['claude-code', 'codex', 'claude-code', 'codex']


@pytest.mark.parametrize('provider', ['codex', 'claude-code'])
def test_fresh_schema_without_added_accounts_reports_unavailable(isolated_db, provider):
    from connections.harness_models import store_success
    from connections.models import upsert_connection
    from infra.database import get_db
    from llm.model_status import scoped_status_rows
    from llm.resolution import resolve_execution_target

    upsert_connection(type='cli', provider=provider, status='available', config={'binary': '/bin/test'})
    db = get_db()
    try:
        store_success(provider, [_row('fresh-default')], harness_version='1', source='test', timestamp='t', db=db)
        db.execute("UPDATE connections SET status='needs_auth' WHERE provider=?", (provider,))
        db.execute("UPDATE vendor_accounts SET state='signed_out' WHERE is_default=1")
        db.commit()
        assert db.execute('SELECT COUNT(*) FROM vendor_accounts WHERE is_default=0').fetchone()[0] == 0
        target = resolve_execution_target(provider + '/fresh-default', db=db)
        assert not target.available and target.availability_reason == 'connection_unavailable'
        row = next(row for row in scoped_status_rows() if row['id'] == provider + '/fresh-default')
        assert (row['status'], row['status_reason']) == ('unavailable', 'connection_unavailable')
    finally:
        db.close()


def test_provider_gate_refuses_missing_binary_and_no_added_signin(accounts):
    from connections.models import provider_may_run
    from connections.vendor_accounts import get_account, set_account_state

    db, default, first, second = accounts
    db.execute("UPDATE connections SET status='not_installed' WHERE provider='codex'")
    db.commit()
    assert not provider_may_run('codex', db=db)
    db.execute("UPDATE connections SET status='needs_auth' WHERE provider='codex'")
    db.commit()
    assert provider_may_run('codex', db=db)
    for account in (default, first, second):  # the Default is an account like the others: no sign-in at all, no run
        current = get_account(account['id'], db=db)
        set_account_state(current['id'], 'signed_out', expected_revision=current['revision'], db=db)
    assert not provider_may_run('codex', db=db)


def test_existing_ready_discovery_is_projected_without_rediscovery(accounts):
    from connections.account_evidence import mark_discovery
    from connections.account_receipts import capture_receipt

    db, _, account, _ = accounts
    receipt = capture_receipt('codex', account_id=account['id'], db=db)
    assert mark_discovery(account['id'], receipt=receipt, status='ready',
                          models={'codex/old-added': _row('old-added')}, db=db)
    db.commit()
    assert _projected('codex', 'old-added')['id'] == 'codex/old-added'
    assert db.execute("SELECT id FROM models WHERE id='codex/old-added'").fetchone() is None


def test_default_and_added_accounts_keep_separate_availability(accounts):
    db, default, account, _ = accounts
    # The Default lists the model unavailable through its own refresh, like any account.
    _discover(db, 'codex', default, [_row('default-unavailable', False)])
    assert db.execute("SELECT available FROM models WHERE id='codex/default-unavailable'").fetchone()[0] == 0
    _discover(db, 'codex', account, [_row('added-only'), _row('default-unavailable')])
    # The row is shared: the added account lists it available, so it is (design D3 item 5, Writer and Availability).
    assert db.execute("SELECT available FROM models WHERE id='codex/default-unavailable'").fetchone()[0] == 1
    extra = {entry['account_id']: entry for entry in _projected('codex', 'added-only')['runs_on']}
    assert (extra[default['id']]['status'], extra[default['id']]['status_reason']) == ('unavailable', 'account_model_not_discovered')
    assert extra[account['id']]['status'] == 'ready'
    shared = {entry['account_id']: entry for entry in _projected('codex', 'default-unavailable')['runs_on']}
    assert (shared[default['id']]['status'], shared[default['id']]['status_reason']) == ('unavailable', 'discovered_model_unavailable')
    assert shared[account['id']]['status'] == 'ready'


@pytest.mark.parametrize('state', ['signed_out', 'removed'])
def test_added_only_rows_disappear_when_account_leaves(accounts, state):
    from connections.vendor_accounts import get_account, set_account_state
    from llm.model_status import scoped_status_rows

    db, _, account, _ = accounts
    _discover(db, 'codex', account, [_row('added-only')])
    current = get_account(account['id'], db=db)
    set_account_state(account['id'], state, expected_revision=current['revision'], db=db)
    assert not any(item['id'] == 'codex/added-only' for item in scoped_status_rows())


@pytest.mark.parametrize('provider', ['codex', 'claude-code'])
def test_added_only_model_resolves_and_binds_with_default_signed_out(accounts, provider):
    from connections.account_holds import refresh_model_hold
    from connections.account_selection import bind_model_account
    from connections.models import upsert_connection
    from connections.vendor_accounts import add_account, list_accounts, set_account_state
    from infra.send_target import model_admission
    from llm.resolution import resolve_execution_target
    from infra.database import write_transaction

    db, default, account, _ = accounts
    if provider == 'claude-code':
        upsert_connection(type='cli', provider=provider, status='available', config={'binary': '/bin/test'})
        default = signed_in_default(provider, db=db)
        account = add_account(provider, 'Added')
        account = set_account_state(account['id'], 'signed_in', expected_revision=account['revision'])
    db.execute("UPDATE connections SET status='needs_auth' WHERE provider=?", (provider,))
    db.commit()
    _discover(db, provider, account, [_row('added-only')])
    target = resolve_execution_target(provider + '/added-only', db=db)
    assert target.available and target.harness == provider
    assert model_admission(target.catalog_id, db=db, thread_id='destination') == 'run'
    assert bind_model_account('destination', target.catalog_id, account_id=account['id'], db=db) == account['id']
    db.execute("UPDATE threads SET model=? WHERE id='destination'", (target.catalog_id,))
    db.commit()
    from connections import read_account_readiness
    assert read_account_readiness(['destination'], db=db)['destination']['ready']
    with write_transaction(db, scope='test_account_hold'):
        assert refresh_model_hold('destination', target.catalog_id, wake=False, db=db) is None
    bind_model_account('actor', target.catalog_id, account_id=default['id'], db=db)
    with write_transaction(db, scope='test_account_hold'):
        assert refresh_model_hold('actor', target.catalog_id, wake=False, db=db) == 'account_model_not_discovered'


def test_added_only_admission_recovers_after_signing_in_and_rediscovery(accounts):
    from connections.vendor_accounts import get_account, set_account_state
    from infra.send_target import model_admission

    db, _, account, _ = accounts
    _discover(db, 'codex', account, [_row('added-only')])
    assert model_admission('codex/added-only', db=db, thread_id='destination') == 'run'
    current = get_account(account['id'], db=db)
    signed_out = set_account_state(current['id'], 'signed_out', expected_revision=current['revision'], db=db)
    assert model_admission('codex/added-only', db=db, thread_id='destination') == 'fail'
    signed_in = set_account_state(signed_out['id'], 'signed_in', expected_revision=signed_out['revision'], db=db)
    _discover(db, 'codex', signed_in, [_row('added-only')])
    assert model_admission('codex/added-only', db=db, thread_id='destination') == 'run'


def test_superseded_discovery_never_projects_rows(accounts):
    from connections.account_discovery_commit import commit_discovery_bundle
    from connections.account_receipts import capture_receipt
    from connections.vendor_accounts import set_account_state
    from llm.model_status import scoped_status_rows

    db, _, account, _ = accounts
    receipt = capture_receipt('codex', account_id=account['id'], db=db)
    set_account_state(account['id'], 'signed_out', expected_revision=account['revision'])
    result = commit_discovery_bundle('codex', SimpleNamespace(usage_report=None, cache_stage=None, unlisted_errors={}),
        [_row('stale')], context=SimpleNamespace(receipt=receipt, account_id=account['id']),
        harness_version='1', db=db)
    assert result.cause == 'account_refresh_superseded'
    assert not any(item['id'] == 'codex/stale' for item in scoped_status_rows())
