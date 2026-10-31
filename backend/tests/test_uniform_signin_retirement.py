"""Migration retires only the converted vendor's registered continuation."""
import pytest


@pytest.mark.parametrize('converted', ['codex', 'claude-code'])
def test_upgrade_retires_converted_vendor_only(isolated_db, converted):
    from infra.database import get_db
    from infra.migration_166_to_167_uniform_signin import migrate_166_to_167
    from tests.registry_helpers import make_session, seed_artifact

    db = get_db()
    owner = make_session(db)
    other = 'claude-code' if converted == 'codex' else 'codex'
    model = 'claude-code/opus' if other == 'claude-code' else 'codex/gpt-5-codex'
    db.execute('UPDATE threads SET model=? WHERE id=?', (model, owner))
    connection_id = 'test-codex' if converted == 'codex' else 'test-cc'
    db.execute("INSERT INTO vendor_accounts(id,connection_id,name,is_default,state,created_at,updated_at) "
               "VALUES ('old',?,'Default',1,'legacy','t','t')", (connection_id,))
    db.execute("INSERT INTO thread_vendor_accounts(thread_id,provider,account_id) VALUES (?,?,'old')", (owner, converted))
    for provider in (converted, other):
        seed_artifact(db, artifact_id=provider, integration=provider,
                      type='thread' if provider == 'codex' else 'session',
                      external_id=provider, parent_thread_id=owner)
    db.commit()
    before = db.execute('SELECT runtime_context_epoch FROM threads WHERE id=?', (owner,)).fetchone()[0]
    migrate_166_to_167(db)
    states = dict(db.execute('SELECT id,state FROM artifact_identities WHERE id IN (?,?)', (converted, other)))
    assert states == {converted: 'archived', other: 'active'}
    assert db.execute('SELECT runtime_context_epoch FROM threads WHERE id=?', (owner,)).fetchone()[0] == before
    db.close()
