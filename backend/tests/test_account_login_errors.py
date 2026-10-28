"""Lost-login outcomes follow the account captured by the failed Turn."""
import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from vendor_account_fixtures import accounts, claude  # noqa: F401


@pytest.mark.asyncio
async def test_added_claude_failure_signs_out_only_its_account(claude, monkeypatch):
    """The exact expired-login error is the answer, so no status read can keep the account signed in."""
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
    vendor_status = AsyncMock(return_value=(True, {}, None))
    monkeypatch.setattr(account_controls, '_vendor_status', vendor_status)
    default_check = AsyncMock()
    source_check = AsyncMock()
    monkeypatch.setattr(account_controls, 'refresh_default_identity', default_check)
    monkeypatch.setattr(claude_discovery, 'reconcile_turn_auth_failure', source_check)
    reconcile_expired_login('Not logged in · Please run /login', logging.getLogger(__name__), account)
    for task in tuple(__import__('subagents.cc_runtime_errors', fromlist=['_EXPIRED_LOGIN_RECONCILIATION_TASKS'])._EXPIRED_LOGIN_RECONCILIATION_TASKS):
        await task
    assert get_account(claude.other, db=db)['state'] == 'signed_out'
    assert get_account(claude.first, db=db) == before_default
    assert get_connection('claude-code', db=db) == before_source
    vendor_status.assert_not_awaited()
    default_check.assert_not_awaited()
    source_check.assert_not_awaited()


def test_expired_sign_out_leaves_the_default_to_its_source_check(claude):
    from connections.account_lifecycle import sign_out_expired
    from connections.vendor_accounts import get_account

    before = get_account(claude.first, db=claude.db)
    assert sign_out_expired(claude.first, db=claude.db) == before
    assert get_account(claude.first, db=claude.db) == before


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
