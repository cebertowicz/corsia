"""Upgrade ambient accounts without changing their identity or other-vendor sessions."""
import json

import pytest


@pytest.mark.parametrize('provider', ['codex', 'claude-code'])
@pytest.mark.parametrize('shape', ['known', 'referenced', 'seeded', 'removed'])
def test_upgrade_ambient_account(isolated_db, provider, shape):
    from infra.database import get_db
    from infra.migration_166_to_167_uniform_signin import migrate_166_to_167
    from registry.listing import find_for_thread
    from registry.models import register_artifact

    db = get_db()
    connection_id = 'test-codex' if provider == 'codex' else 'test-cc'
    identity = {'default_comparison': {'auth_state': 'known', 'facts': {'email': 'known@example.test'}}} if shape == 'known' else {}
    state = 'removed' if shape == 'removed' else 'legacy'
    db.execute("INSERT INTO vendor_accounts(id,connection_id,name,is_default,state,identity_json,created_at,updated_at) "
               "VALUES ('old',?,'Default',1,?,?, 't','t')", (connection_id, state, json.dumps(identity)))
    db.execute("INSERT INTO threads(id,user_id,model,created_at,updated_at) VALUES ('cross','u','codex/gpt-5-codex','t','t')")
    if shape == 'referenced':
        db.execute("INSERT INTO thread_vendor_accounts(thread_id,provider,account_id) VALUES ('cross',?,'old')", (provider,))
    session_type = 'thread' if provider == 'codex' else 'session'
    register_artifact(integration=provider, type=session_type, external_id='provider-session',
                      parent_thread_id='cross', path='/synthetic/session', metadata={}, db=db)
    db.commit()
    assert find_for_thread('cross', provider, session_type, db=db) is not None
    before = db.execute("SELECT runtime_context_epoch FROM threads WHERE id='cross'").fetchone()[0]
    migrate_166_to_167(db)
    row = db.execute("SELECT * FROM vendor_accounts WHERE id='old'").fetchone()
    if shape in ('known', 'referenced'):
        assert row['state'] == 'signed_out'
        assert row['source_home'] and row['runtime_home']
        assert json.loads(row['identity_json'])['upgrade_reason']
        from api.vendor_accounts import _safe
        from connections.vendor_accounts import get_account
        assert _safe(get_account('old', db=db))['upgrade_reason'] == 'sign_in_to_keep_using_account'
        if shape == 'known':
            assert json.loads(row['identity_json'])['hint']['email'] == 'known@example.test'
    else:
        assert row['state'] == 'removed'
    if shape == 'referenced':
        after = db.execute("SELECT runtime_context_epoch FROM threads WHERE id='cross'").fetchone()[0]
        assert after == before + (provider == 'codex')
        assert find_for_thread('cross', provider, session_type, db=db) is None
    snapshot = tuple(row)
    migrate_166_to_167(db)
    assert tuple(db.execute("SELECT * FROM vendor_accounts WHERE id='old'").fetchone()) == snapshot
    db.close()
