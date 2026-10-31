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
        vendor, SimpleNamespace(usage_report=None, cache_stage=None),
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


async def _recovery_settled(previous):
    """Wait for the held-input recovery a discovery commit wakes on the app loop."""
    from infra import unavailable_model_recovery as recovery

    for _ in range(12):
        await asyncio.sleep(0)
        if recovery._TASK is not previous:
            break
    if recovery._TASK is not None:
        await asyncio.wait_for(asyncio.shield(recovery._TASK), timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize('vendor', ['codex', 'claude-code'])
async def test_fresh_install_send_before_any_sign_in_runs_after_sign_in(
        isolated_db, notification_owner, vendor):
    """A fresh install has no account: a send waits, held ``account_not_configured``, and the
    first sign-in plus its discovery releases and claims it, as the hold's copy promises."""
    from connections.account_discovery_commit import commit_discovery_bundle
    from connections.account_receipts import capture_receipt
    from connections.account_signin_commit import commit_signed_in
    from connections.models import upsert_connection
    from connections.vendor_accounts import add_account
    from infra import unavailable_model_recovery as recovery
    from infra.database import get_db
    from infra.input_admission import accept_thread_input
    from infra.thread_execution import drive_thread
    from infra.unavailable_model_recovery import held_head_kind_sql

    selector = {'codex': 'gpt-5-codex', 'claude-code': 'opus'}[vendor]
    model = f'{vendor}/{selector}'
    db = get_db()
    # Before any sign-in the harness shows only its placeholder row, and a chat may still pick it.
    db.execute('DELETE FROM models WHERE harness=? AND id<>?', (vendor, model))
    db.execute("UPDATE models SET harness=?,provider=?,provider_model_id=?,source='legacy_fallback' WHERE id=?",
               (vendor, vendor, selector, model))
    db.commit()
    upsert_connection(type='cli', provider=vendor, status='available', config={'binary': '/bin/journey'})
    assert db.execute('SELECT COUNT(*) FROM vendor_accounts').fetchone()[0] == 0
    db.execute("INSERT INTO threads(id,user_id,model,created_at,updated_at) VALUES ('fresh','user',?,'t','t')",
               (model,))
    db.commit()

    def held_kind():
        return db.execute(f"SELECT {held_head_kind_sql(':thread')} AS kind", {'thread': 'fresh'}).fetchone()['kind']

    accept_thread_input('fresh', role='user', content='please run', intent='apply_now', message_id='sent', db=db)
    db.commit()
    await drive_thread('fresh')
    row = dict(db.execute('SELECT active_turn_id,delivery_state,delivery_error,write_outcome '
                          'FROM messages WHERE id=?', ('sent',)).fetchone())
    assert notification_owner == []
    assert row['active_turn_id'] is None and row['write_outcome'] == 'proved_no_write'
    assert row['delivery_error'] == 'account_not_configured' and row['delivery_state'] != 'failed'
    # Plans and recovery read it as an account hold, so the chat shows it waiting for sign-in.
    assert held_kind() == 'account'
    assert db.execute("SELECT COUNT(*) FROM thread_vendor_accounts WHERE thread_id='fresh'").fetchone()[0] == 0

    # Settings > Providers: add the account, sign in, then the login's first discovery commits.
    account = add_account(vendor, 'Default', db=db)
    db.commit()
    if vendor == 'codex':
        (Path(account['source_home']) / 'auth.json').write_text(json.dumps({
            'auth_mode': 'chatgpt', 'tokens': {'access_token': '[REDACTED]'}}))
    identity = {'account_fp': 'fresh'} if vendor == 'codex' else {'email': 'fresh@example.test'}
    commit_signed_in(account['id'], account['revision'], identity)
    receipt = capture_receipt(vendor, account_id=account['id'], db=db)
    previous = recovery._TASK
    result = commit_discovery_bundle(
        vendor, SimpleNamespace(usage_report=None, cache_stage=None),
        [{'selector_id': selector, 'available': True}],
        context=SimpleNamespace(receipt=receipt, account_id=account['id']), harness_version='1', db=db)
    assert result.status == 'ready'
    await _recovery_settled(previous)

    assert len(notification_owner) == 1
    claimed = notification_owner[0]
    assert [item.message_id for item in claimed.items] == ['sent']
    # It runs on the chat's saved model, on the account just signed in, with no switch.
    assert claimed.target_model == model
    assert db.execute("SELECT model FROM threads WHERE id='fresh'").fetchone()[0] == model
    assert db.execute("SELECT account_id FROM thread_vendor_accounts WHERE thread_id='fresh'").fetchone()[0] \
        == account['id']
    after = dict(db.execute('SELECT active_turn_id,delivery_error FROM messages WHERE id=?', ('sent',)).fetchone())
    assert after['active_turn_id'] == claimed.turn_id and after['delivery_error'] is None
    assert held_kind() is None
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('vendor', ['codex', 'claude-code'])
async def test_source_state_follows_the_accounts_through_sign_in_and_removal(isolated_db, vendor):
    """First-run setup and Settings read the vendor source state: it follows the account, not a restart."""
    from connections.account_discovery_commit import commit_discovery_bundle
    from connections.account_receipts import capture_receipt
    from connections.account_signin_commit import commit_signed_in
    from connections.source_state import NEEDS_AUTH, READY, harness_state
    from connections.vendor_accounts import get_account, set_account_state
    from infra.database import get_db
    from infra.migration_166_to_167_uniform_signin import migrate_166_to_167
    from llm.local_setup_gates import subscription_available

    db = get_db()
    _pre_upgrade_install(db, vendor, 'needs_auth')
    migrate_166_to_167(db)
    db.commit()
    account = get_account('ambient', db=db)
    # The upgrade leaves the vendor needing sign-in, and no subscription for setup to count.
    assert await subscription_available() is None

    if vendor == 'codex':
        (Path(account['source_home']) / 'auth.json').write_text(json.dumps({
            'auth_mode': 'chatgpt', 'tokens': {'access_token': 'journey'}}))
    identity = {'account_fp': 'journey'} if vendor == 'codex' else {'email': 'journey@example.test'}
    commit_signed_in('ambient', account['revision'], identity)
    # A login with no discovery proves nothing yet.
    assert harness_state(vendor, db=db)['state'] != READY

    receipt = capture_receipt(vendor, account_id='ambient', db=db)
    result = commit_discovery_bundle(
        vendor, SimpleNamespace(usage_report=None, cache_stage=None),
        [{'selector_id': 'journey', 'available': True}],
        context=SimpleNamespace(receipt=receipt, account_id='ambient'), harness_version='1', db=db)
    assert result.status == 'ready'
    assert harness_state(vendor, db=db)['state'] == READY
    source = await subscription_available()
    assert source is not None and source.provider == vendor

    current = get_account('ambient', db=db)
    set_account_state('ambient', 'removed', expected_revision=current['revision'], db=db)
    db.commit()
    assert harness_state(vendor, db=db)['state'] == NEEDS_AUTH
    assert await subscription_available() is None
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('vendor', ['codex', 'claude-code'])
async def test_an_installed_source_with_only_a_signed_out_account_counts_for_no_subscription(isolated_db, vendor):
    """An upgrade leaves an installed source with only a signed-out account: setup must not skip the local model on it."""
    from infra.database import get_db
    from infra.migration_166_to_167_uniform_signin import migrate_166_to_167
    from llm.local_setup_gates import limited_subscription_message, subscription_available

    db = get_db()
    _pre_upgrade_install(db, vendor, 'available')
    db.commit()
    migrate_166_to_167(db)
    db.commit()
    assert db.execute("SELECT state FROM vendor_accounts WHERE id='ambient'").fetchone()[0] == 'signed_out'
    assert await subscription_available() is None
    assert await limited_subscription_message() is None
    db.close()
