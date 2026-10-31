"""A harness model is available only while a signed-in account's current ready discovery lists it."""
import pytest

from vendor_account_fixtures import accounts  # noqa: F401
from test_added_account_catalog import _discover, _row


@pytest.mark.parametrize('change', ['new_login', 'invalidated'])
def test_a_login_change_that_stays_signed_in_withdraws_only_that_accounts_models(accounts, change):
    """Neither change passes through signed_in -> signed_out, so nothing but the derivation can see it."""
    from connections.account_evidence import discovery_record, invalidate_discovery
    from connections.vendor_accounts import get_account, set_account_state
    from llm.harness_catalog import harness_row
    from llm.resolution import resolve_execution_target

    db, _, first, second = accounts
    _discover(db, 'codex', first, [_row('only-first'), _row('shared')])
    _discover(db, 'codex', second, [_row('shared')])
    assert resolve_execution_target('codex/only-first', db=db).available

    account = get_account(first['id'], db=db)
    if change == 'new_login':
        set_account_state(account['id'], 'signed_in', expected_revision=account['revision'], db=db)
    else:
        record = discovery_record(account)
        invalidate_discovery(account, login_seq=record['login_seq'], auth_state=record['auth_state'], db=db)
    db.commit()
    assert get_account(first['id'], db=db)['state'] == 'signed_in'

    assert not harness_row('codex/only-first', db=db)['available']
    assert not resolve_execution_target('codex/only-first', db=db).available
    assert harness_row('codex/shared', db=db)['available']
    assert resolve_execution_target('codex/shared', db=db).available


def test_an_upgraded_row_no_signed_in_account_lists_reads_unavailable(isolated_db):
    """A row an older build stored as available, with its window, says nothing once no account lists it."""
    import json

    from connections.models import upsert_connection
    from infra.database import get_db
    from llm.harness_catalog import harness_row
    from llm.resolution import resolve_execution_target

    upsert_connection(type='cli', provider='codex', status='available', config={'binary': '/bin/test'})
    db = get_db()
    try:
        db.execute(
            "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,display_name,backend,source,"
            "enabled,available,context_window,supports_vision,supports_reasoning,metadata_json,created_at,updated_at) "
            "VALUES ('codex/upgraded','codex','codex','upgraded','codex/upgraded','Upgraded','codex',"
            "'account_discovery',1,1,950000,1,1,?,'t','t')",
            (json.dumps({'resolved_identity': 'upgraded', 'discovered_capabilities': {'effort_levels': ['low']}}),))
        db.commit()
        assert not db.execute("SELECT COUNT(*) FROM vendor_accounts WHERE state='signed_in'").fetchone()[0]

        row = harness_row('codex/upgraded', db=db)
        assert (row['available'], row['context_window'], row['supports_vision'], row['supports_reasoning']) == (
            0, None, 0, 0)
        meta = json.loads(row['metadata_json'])
        assert 'resolved_identity' not in meta and 'discovered_capabilities' not in meta
        assert meta['availability_reason'] == 'not_returned_by_discovery'
        assert not resolve_execution_target('codex/upgraded', db=db).available
    finally:
        db.close()


def test_a_runtime_window_applies_only_while_its_identity_is_listed(accounts):
    """The observed window beats a larger listed one for the same model, and stops once the selector names another."""
    from connections.harness_models import record_runtime_context_observation
    from llm.harness_catalog import harness_row

    db, _, first, _ = accounts
    _discover(db, 'codex', first, [_row('observed')])
    assert harness_row('codex/observed', db=db)['context_window'] == 200000
    assert record_runtime_context_observation('codex', selector='observed', reported_identity='observed',
                                              effective_tokens=150000, source='test', db=db) is not None
    db.commit()
    assert harness_row('codex/observed', db=db)['context_window'] == 150000

    _discover(db, 'codex', first, [{**_row('observed'), 'resolved_identity': 'observed-next'}])
    row = harness_row('codex/observed', db=db)
    assert row['context_window'] == 200000
    assert 'context_observation' not in row['metadata_json']
