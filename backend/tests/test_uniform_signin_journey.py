"""J5: an upgraded install's sends wait for sign-in, then run on the account's own home.

Design D1 A: the upgrade converts the ambient Default to a signed-out account on its own
homes; that vendor's sends wait, held ``account_signed_out``, never failed. Signing in
through the real commit path and committing a discovery releases the held input, and the
launch it gets uses the account's own homes.
"""
import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from vendor_account_fixtures import notification_owner  # noqa: F401

MODELS = {'codex': 'codex/journey', 'claude-code': 'claude-code/journey'}


def _pre_upgrade_install(db, vendor, status):
    """An install at schema 166: an ambient Default, a thread bound to it, one available model."""
    from connections.models import upsert_connection

    # The shared fixture's placeholder rows take the shape a real install's fallback rows have.
    for provider, selector in (('codex', 'gpt-5-codex'), ('claude-code', 'opus')):
        db.execute("UPDATE models SET harness=?,provider=?,provider_model_id=?,source='legacy_fallback' WHERE id=?",
                   (provider, provider, selector, f'{provider}/{selector}'))
    db.commit()
    upsert_connection(type='cli', provider=vendor, status=status, config={'binary': '/bin/journey'})
    connection = db.execute('SELECT id FROM connections WHERE provider=?', (vendor,)).fetchone()[0]
    evidence = {'default_comparison': {'auth_state': 'known', 'facts': {'email': 'journey@example.test'}}}
    db.execute("INSERT INTO vendor_accounts(id,connection_id,name,is_default,state,identity_json,created_at,updated_at) "
               "VALUES ('ambient',?,'Default',1,'legacy',?,'t','t')", (connection, json.dumps(evidence)))
    db.execute("INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,"
               "enabled,available,created_at,updated_at) VALUES (?,?,?,?,?,?,1,1,'t','t')",
               (MODELS[vendor], 'Journey', MODELS[vendor], vendor, 'journey', vendor))
    db.execute("INSERT INTO threads(id,user_id,model,created_at,updated_at) VALUES ('journey','user',?,'t','t')",
               (MODELS[vendor],))
    db.execute("INSERT INTO thread_vendor_accounts(thread_id,provider,account_id) VALUES ('journey',?,'ambient')",
               (vendor,))
    db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['available', 'needs_auth', 'unverified'])
@pytest.mark.parametrize('vendor', ['codex', 'claude-code'])
async def test_upgraded_send_waits_then_runs_on_the_accounts_own_home(
        isolated_db, notification_owner, vendor, status):
    from connections.account_discovery_commit import commit_discovery_bundle
    from connections.account_receipts import capture_receipt
    from connections.account_runtime import homes_for, thread_launch_homes
    from connections.account_signin_commit import commit_signed_in
    from connections.vendor_accounts import get_account
    from infra import unavailable_model_recovery as recovery
    from infra.database import get_db
    from infra.input_admission import accept_thread_input
    from infra.migration_166_to_167_uniform_signin import migrate_166_to_167
    from infra.thread_execution import drive_thread

    db = get_db()
    _pre_upgrade_install(db, vendor, status)
    migrate_166_to_167(db)
    db.commit()
    account = get_account('ambient', db=db)
    assert account['state'] == 'signed_out' and account['source_home'] and account['runtime_home']

    accept_thread_input('journey', role='user', content='please run', intent='apply_now',
                        message_id='sent', db=db)
    db.commit()
    await drive_thread('journey')
    row = dict(db.execute('SELECT active_turn_id,delivery_state,delivery_error,write_outcome '
                          'FROM messages WHERE id=?', ('sent',)).fetchone())
    # Held, not failed: nothing was claimed or written, and the cause names the sign-in.
    assert notification_owner == []
    assert row['active_turn_id'] is None and row['write_outcome'] == 'proved_no_write'
    assert row['delivery_error'] == 'account_signed_out' and row['delivery_state'] != 'failed'

    if vendor == 'codex':
        (Path(account['source_home']) / 'auth.json').write_text(json.dumps({
            'auth_mode': 'chatgpt', 'tokens': {'access_token': '[REDACTED]'}}))
    signed_in, receipt = commit_signed_in('ambient', account['revision'], {'account_fp': 'journey'}) \
        if vendor == 'codex' else commit_signed_in('ambient', account['revision'], {'email': 'journey@example.test'})
    assert signed_in is None and receipt is not None
    # Signing in alone does not run it: this login has no discovery yet, so the input still waits.
    await drive_thread('journey')
    assert notification_owner == []

    receipt = capture_receipt(vendor, account_id='ambient', db=db)
    previous = recovery._TASK
    result = commit_discovery_bundle(
        vendor, SimpleNamespace(usage_report=None, cache_stage=None, unlisted_errors={}),
        [{'selector_id': 'journey', 'available': True}],
        context=SimpleNamespace(receipt=receipt, account_id='ambient'), harness_version='1', db=db)
    assert result.status == 'ready'
    # The commit wakes held-input recovery on the app loop and that task releases the input; wait for it
    # (a rerun coalesces into the task already running) rather than racing it with a drive of our own.
    for _ in range(12):
        await asyncio.sleep(0)
        if recovery._TASK is not previous:
            break
    if recovery._TASK is not None:
        await asyncio.wait_for(asyncio.shield(recovery._TASK), timeout=5)
    assert len(notification_owner) == 1
    claimed = notification_owner[0]
    assert [item.message_id for item in claimed.items] == ['sent']
    after = dict(db.execute('SELECT active_turn_id,delivery_error FROM messages WHERE id=?', ('sent',)).fetchone())
    assert after['active_turn_id'] == claimed.turn_id and after['delivery_error'] is None

    # The launch reads the account's own homes, the ones the upgrade gave it.
    homes = thread_launch_homes('journey', vendor, turn_id=claimed.turn_id, db=db)
    assert homes == homes_for(get_account('ambient', db=db))
    if vendor == 'codex':
        assert (homes.codex_source, homes.codex_runtime) == (account['source_home'], account['runtime_home'])
    else:
        assert homes.claude_dir == account['source_home']
    db.close()
