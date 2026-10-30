"""Re-login rechecks the captured account and reports refresh outcomes."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vendor_account_fixtures import accounts  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize(('status', 'cause', 'expected'), [
    ('ready', None, 'already_signed_in'),
    ('failed', 'discovery_failed', 'model_refresh_failed'),
    ('failed', 'account_refresh_superseded', 'superseded'),
])
async def test_signed_in_relogin_refresh_result(accounts, monkeypatch, caplog, status, cause, expected):
    from connections import account_controls, harness_models

    _, _, account, _ = accounts
    monkeypatch.setattr(account_controls, '_binary', AsyncMock(return_value='/synthetic'))
    monkeypatch.setattr(account_controls, '_vendor_status', AsyncMock(return_value=(True, {}, None)))
    monkeypatch.setattr(harness_models, 'refresh_provider', AsyncMock(return_value=SimpleNamespace(status=status, cause=cause)))
    spawn = AsyncMock()
    monkeypatch.setattr(account_controls, '_spawn', spawn)
    result = await account_controls.start_signin(account['id'])
    assert result['status'] == expected
    if expected == 'model_refresh_failed':
        assert result['cause'] == 'account_refresh_failed' and result['ref']
    if expected == 'superseded':
        assert 'account model refresh failed' not in caplog.text
        assert result['cause'] == 'account_refresh_superseded'
    spawn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('skip_reason', ['codex_auth_projection_changed', 'codex_auth_source_missing', 'no_receipt'])
async def test_signed_in_account_without_refresh_starts_vendor_login(accounts, monkeypatch, skip_reason):
    from connections import account_controls, harness_models

    _, _, account, _ = accounts
    monkeypatch.setattr(account_controls, '_binary', AsyncMock(return_value='/synthetic'))
    monkeypatch.setattr(account_controls, '_vendor_status', AsyncMock(return_value=(True, {}, None)))
    monkeypatch.setattr(account_controls, 'readiness_reason', lambda _: None if skip_reason == 'no_receipt' else skip_reason)
    monkeypatch.setattr(account_controls, 'account_receipt', lambda _: None if skip_reason == 'no_receipt' else {'revision': 1})
    refresh = AsyncMock()
    monkeypatch.setattr(harness_models, 'refresh_provider', refresh)
    spawn = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(account_controls, '_spawn', spawn)
    monkeypatch.setattr(account_controls, '_hold', AsyncMock())
    monkeypatch.setattr(account_controls, '_run', AsyncMock(return_value=(0, b'')))
    result = await account_controls.start_signin(account['id'])
    assert result['status'] == 'waiting'
    spawn.assert_awaited_once()
    refresh.assert_not_awaited()
    await account_controls._SIGNINS[account['id']].task
    account_controls._SIGNINS.pop(account['id'])


@pytest.mark.asyncio
async def test_unreadable_signed_in_status_has_logged_reference(accounts, monkeypatch, caplog):
    from connections import account_controls

    _, _, account, _ = accounts
    monkeypatch.setattr(account_controls, '_binary', AsyncMock(return_value='/synthetic'))
    monkeypatch.setattr(account_controls, '_vendor_status', AsyncMock(return_value=(None, {}, 'vendor_status_unreadable')))
    result = await account_controls.start_signin(account['id'])
    assert result['status'] == 'unavailable'
    assert result['cause'] == 'vendor_status_unreadable'
    assert result['ref']
    assert caplog.text.count(f"reference={result['ref']} cause=vendor_status_unreadable") == 1


@pytest.mark.asyncio
async def test_signed_in_github_account_refuses_without_logout_or_login(monkeypatch):
    from connections import account_controls
    from connections.vendor_accounts import AccountError

    monkeypatch.setattr(account_controls, 'custom_account', AsyncMock(return_value={
        'id': 'gh', 'provider': 'github', 'state': 'signed_in', 'revision': 1, 'source_home': '/synthetic',
    }))
    monkeypatch.setattr(account_controls, 'signin_recheck', AsyncMock(return_value=None))
    monkeypatch.setattr(account_controls, '_binary', AsyncMock(return_value='/synthetic'))
    logout = AsyncMock(return_value=(0, b''))
    spawn = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(account_controls, '_run', logout)
    monkeypatch.setattr(account_controls, '_spawn', spawn)
    monkeypatch.setattr(account_controls, '_hold', AsyncMock())
    with pytest.raises(AccountError, match='account is already signed in'):
        await account_controls.start_signin('gh')
    logout.assert_not_awaited()
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_signed_in_account_found_signed_out_starts_login(accounts, monkeypatch):
    from connections import account_controls
    from connections.vendor_accounts import get_account

    db, _, account, _ = accounts
    monkeypatch.setattr(account_controls, '_binary', AsyncMock(return_value='/synthetic'))
    monkeypatch.setattr(account_controls, '_vendor_status', AsyncMock(return_value=(False, {}, None)))
    monkeypatch.setattr(account_controls, '_spawn', AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(account_controls, '_hold', AsyncMock())
    monkeypatch.setattr(account_controls, '_run', AsyncMock(return_value=(0, b'')))
    result = await account_controls.start_signin(account['id'])
    assert result['status'] == 'waiting'
    assert get_account(account['id'], db=db)['state'] == 'signed_out'
    await account_controls._SIGNINS[account['id']].task
    account_controls._SIGNINS.pop(account['id'])
