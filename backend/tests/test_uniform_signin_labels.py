"""Converted Default labels retain last-known facts only until sign-in."""
import json

import pytest


@pytest.mark.parametrize('provider,connection_id', [('codex', 'test-codex'), ('claude-code', 'test-cc')])
def test_converted_default_label_through_signin_and_rename(isolated_db, monkeypatch, provider, connection_id):
    from connections import harness_usage
    from connections.account_profiles import account_label
    from connections.account_signin_commit import commit_signed_in
    from connections.vendor_accounts import comparison_facts, get_account
    from infra.database import get_db
    from infra.migration_166_to_167_uniform_signin import migrate_166_to_167
    from vendor_account_fixtures import seed_usage

    db = get_db()
    old = {'default_comparison': {'auth_state': 'known', 'facts': {
        'email': 'old@example.test', 'plan': 'Pro', 'account_fp': 'old-fingerprint'}}}
    db.execute("INSERT INTO vendor_accounts(id,connection_id,name,is_default,state,identity_json,created_at,updated_at) "
               "VALUES ('old',?,'Default',1,'legacy',?,'t','t')", (connection_id, json.dumps(old)))
    db.commit()
    migrate_166_to_167(db)
    db.commit()
    account = get_account('old', db=db)
    assert account['identity']['hint'] == {'email': 'old@example.test', 'plan': 'Pro'}
    assert comparison_facts(account) is None
    label = account_label(account, rows=[account])
    assert label['text'] == 'old@example.test' and label['custom'] is None
    plan_only = {**account, 'identity': {'upgrade_reason': 'sign_in_to_keep_using_account', 'hint': {'plan': 'Pro'}}}
    assert account_label(plan_only, rows=[plan_only])['text'] == ('ChatGPT Pro' if provider == 'codex' else 'Claude Pro')
    assert comparison_facts(plan_only) is None
    seed_usage('old', facts={'synthetic': 'retained'}, db=db)
    clears = []
    clear = harness_usage.clear_account_usage
    def record_clear(*args, **kwargs):
        clears.append(args)
        return clear(*args, **kwargs)
    monkeypatch.setattr(harness_usage, 'clear_account_usage', record_clear)
    commit_signed_in('old', account['revision'], {'email': 'new@example.test', 'account_fp': 'new-fingerprint'})
    # The lifecycle clears usage once for every sign-in; the hint causes no extra identity-change clear.
    assert clears == [(provider, 'old')]
    assert harness_usage.stored_usage(provider=provider, account_id='old', db=db) is None
    account = get_account('old', db=db)
    assert account_label(account, rows=[account])['text'] == 'new@example.test'
    db.execute("UPDATE vendor_accounts SET name='Work' WHERE id='old'")
    db.commit()
    account = get_account('old', db=db)
    label = account_label(account, rows=[account])
    assert label['text'] == 'Work · new@example.test' and label['custom'] == 'Work'
    db.close()
