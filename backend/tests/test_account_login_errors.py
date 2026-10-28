"""Lost-login outcomes follow the account captured by the failed Turn."""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vendor_account_fixtures import accounts, claude  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize('vendor_answer', [False, None])
async def test_added_claude_failure_checks_only_its_account(claude, monkeypatch, vendor_answer):
    from connections import account_controls, claude_discovery
    from connections.models import get_connection
    from connections.vendor_accounts import get_account
    from subagents.cc_runtime_errors import reconcile_expired_login
    from subagents.turn_account_error import turn_account

    db = claude.db
    db.execute("UPDATE active_turns SET vendor_account_id=? WHERE id='claude-turn'", (claude.other,))
    db.commit()
    account = turn_account('claude-turn', 'claude-code', db=db)
    before_default = get_account(claude.first, db=db)
    before_source = get_connection('claude-code', db=db)
    monkeypatch.setattr(account_controls, '_binary', AsyncMock(return_value='/synthetic'))
    monkeypatch.setattr(account_controls, '_vendor_status', AsyncMock(return_value=(vendor_answer, {}, 'unknown' if vendor_answer is None else None)))
    default_check = AsyncMock()
    source_check = AsyncMock()
    monkeypatch.setattr(account_controls, 'refresh_default_identity', default_check)
    monkeypatch.setattr(claude_discovery, 'reconcile_turn_auth_failure', source_check)
    reconcile_expired_login('Not logged in · Please run /login', logging.getLogger(__name__), account)
    for task in tuple(__import__('subagents.cc_runtime_errors', fromlist=['_EXPIRED_LOGIN_RECONCILIATION_TASKS'])._EXPIRED_LOGIN_RECONCILIATION_TASKS):
        await task
    assert get_account(claude.other, db=db)['state'] == ('signed_out' if vendor_answer is False else 'signed_in')
    assert get_account(claude.first, db=db) == before_default
    assert get_connection('claude-code', db=db) == before_source
    default_check.assert_not_awaited()
    source_check.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_failure_keeps_provider_recheck(claude, monkeypatch):
    from connections import claude_discovery
    from subagents.cc_runtime_errors import reconcile_expired_login
    from subagents.turn_account_error import turn_account

    check = AsyncMock()
    monkeypatch.setattr(claude_discovery, 'reconcile_turn_auth_failure', check)
    account = turn_account('claude-turn', 'claude-code', db=claude.db)
    reconcile_expired_login('Not logged in · Please run /login', logging.getLogger(__name__), account)
    await asyncio.sleep(0)
    check.assert_awaited_once_with('Not logged in · Please run /login')


@pytest.mark.asyncio
@pytest.mark.parametrize('status,refresh,expected', [
    (True, True, 'already_signed_in'),
    (True, False, 'model_refresh_failed'),
    (None, None, 'unavailable'),
])
async def test_signin_rechecks_before_touching_login(accounts, monkeypatch, status, refresh, expected):
    from connections import account_controls, harness_models

    _, _, row, _ = accounts
    monkeypatch.setattr(account_controls, '_binary', AsyncMock(return_value='/synthetic'))
    monkeypatch.setattr(account_controls, '_vendor_status', AsyncMock(return_value=(status, {}, 'unreadable' if status is None else None)))
    monkeypatch.setattr(account_controls, 'refresh_default_identity', AsyncMock())
    monkeypatch.setattr(harness_models, 'refresh_provider', AsyncMock(return_value=SimpleNamespace(status='ready' if refresh else 'failed', cause='failed')))
    spawn = AsyncMock()
    run = AsyncMock()
    monkeypatch.setattr(account_controls, '_spawn', spawn)
    monkeypatch.setattr(account_controls, '_run', run)
    result = await account_controls.start_signin(row['id'])
    assert result['status'] == expected
    if refresh is False:
        assert result['ref'] and result['cause'] == 'account_refresh_failed'
    if status is None:
        assert result['cause'] == 'unreadable'
    spawn.assert_not_awaited()
    run.assert_not_awaited()


def test_error_messages_use_turn_account_and_reference(claude, accounts, caplog):
    from subagents.codex_runtime_errors import CodexModelCacheChanged
    from subagents.turn_account_error import claude_login_message, turn_account

    db = claude.db
    db.execute("UPDATE active_turns SET vendor_account_id=? WHERE id='claude-turn'", (claude.other,))
    db.commit()
    account = turn_account('claude-turn', 'claude-code', db=db)
    assert claude_login_message(account) == 'Other is signed out of Claude Code. Sign it in again under Settings › Providers.'
    assert claude_login_message({'custom': False}) == 'Claude Code is signed out on this computer. Sign in again under Settings › Providers.'
    codex = accounts[2]
    db.execute("INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,boot_epoch,vendor_account_id) "
               "VALUES ('codex-turn','destination',0,'codex/test','running','t','test',?)", (codex['id'],))
    db.commit()
    assert str(CodexModelCacheChanged()) == 'account_model_cache_changed'
