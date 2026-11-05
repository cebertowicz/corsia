"""One disposable 6,000-thread rehearsal of the uniform sign-in upgrade."""
import json
import os
import sqlite3
import subprocess
import sys


def test_synthetic_upgrade_rehearsal(isolated_db, tmp_path):
    from infra.database import get_db
    from tests.registry_helpers import seed_artifact

    db = get_db()
    vendors = ('codex', 'claude-code')
    for vendor in vendors:
        connection = 'test-codex' if vendor == 'codex' else 'test-cc'
        for index in range(4):
            identity = f'{vendor}-{index}'
            homes = (None, None) if index == 0 else (f'/synthetic/{identity}/source', f'/synthetic/{identity}/runtime')
            evidence = {'default_comparison': {'auth_state': 'known', 'facts': {'email': 'synthetic@example.test'}}} if index == 0 else {}
            db.execute('INSERT INTO vendor_accounts(id,connection_id,name,is_default,state,source_home,runtime_home,identity_json,created_at,updated_at) '
                       'VALUES (?,?,?,?,?,?,?,?,\'t\',\'t\')',
                       (identity, connection, identity, int(index == 0), 'legacy' if index == 0 else 'signed_in', *homes, json.dumps(evidence)))
    models = ('codex/gpt-5-codex', 'claude-code/opus', 'openai/test-checkpoint')
    db.executemany("INSERT INTO threads(id,user_id,model,created_at,updated_at) VALUES (?,'synthetic',?,'t','t')",
                   [(f'thread-{i}', models[i % 3]) for i in range(6000)])
    for i in range(6000):
        vendor = vendors[i % 2]
        if i % 4 != 0:
            account = f'{vendor}-{0 if i % 4 in (1, 2) else 1}'
            db.execute('INSERT INTO thread_vendor_accounts(thread_id,provider,account_id) VALUES (?,?,?)',
                       (f'thread-{i}', vendor, account))
        if i < 200:
            db.execute("INSERT INTO messages(id,thread_id,role,content,created_at,delivery_state) VALUES (?,?,'user','synthetic','t','queued')",
                       (f'input-{i}', f'thread-{i}'))
            db.execute("INSERT INTO thread_vendor_account_holds(thread_id,provider,account_id,reason,updated_at) VALUES (?,?,?,'account_signed_out','t')",
                       (f'thread-{i}', vendor, f'{vendor}-0'))
            seed_artifact(db, artifact_id=f'session-{i}', integration=vendor,
                          type='thread' if vendor == 'codex' else 'session',
                          external_id=f'provider-{i}', parent_thread_id=f'thread-{i}')
    # The shared test fixture's model rows are synthetic; make the two startup
    # placeholders match the persisted fallback shape a real install carries.
    for provider, selector in (('codex', 'gpt-5-codex'), ('claude-code', 'opus')):
        db.execute("UPDATE models SET harness=?,provider=?,provider_model_id=?,source='legacy_fallback' WHERE id=?",
                   (provider, provider, selector, f'{provider}/{selector}'))
    db.commit()
    db.execute('PRAGMA user_version=166')
    state = tmp_path / 'startup-state'
    workspace = tmp_path / 'startup-workspace'
    state.mkdir()
    workspace.mkdir()
    target = sqlite3.connect(state / 'corsia.db')
    db.backup(target)
    target.close()
    db.close()
    env = dict(os.environ, CORSIA_STATE_DIR=str(state), CORSIA_WORKSPACE=str(workspace),
               CORSIA_TEST_MODE='1', CORSIA_ANALYTICS_DISABLE='1')
    child = subprocess.run([sys.executable, '-c',
        'import time; from loguru import logger; from app_startup_init import initialize_startup; '
        'start=time.perf_counter(); initialize_startup(logger); '
        'print(f"STARTUP_INITIALIZATION_SECONDS={time.perf_counter()-start:.3f}")'],
        env=env, capture_output=True, text=True, timeout=120)
    assert child.returncode == 0, child.stderr
    print(child.stdout)
    db = sqlite3.connect(state / 'corsia.db')
    accounts = db.execute("SELECT COUNT(*) FROM vendor_accounts WHERE state='signed_out' AND source_home IS NOT NULL").fetchone()[0]
    epochs = db.execute('SELECT COUNT(*) FROM threads WHERE runtime_context_epoch > 0').fetchone()[0]
    archived = db.execute("SELECT COUNT(*) FROM artifact_identities WHERE state='archived'").fetchone()[0]
    queued = db.execute("SELECT COUNT(*) FROM messages WHERE delivery_state='queued'").fetchone()[0]
    print(f'REHEARSAL accounts_converted={accounts} epochs_changed={epochs} sessions_archived={archived} queued_preserved={queued}')
    assert accounts == 2 and epochs > 0 and archived > 0 and queued == 200
    db.close()
