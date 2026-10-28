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
    monkeypatch.setattr(account_controls, 'refresh_default_identity', AsyncMock())
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
async def test_signed_in_account_found_signed_out_starts_login(accounts, monkeypatch):
    from connections import account_controls
    from connections.vendor_accounts import get_account

    db, _, account, _ = accounts
    monkeypatch.setattr(account_controls, '_binary', AsyncMock(return_value='/synthetic'))
    monkeypatch.setattr(account_controls, '_vendor_status', AsyncMock(return_value=(False, {}, None)))
    monkeypatch.setattr(account_controls, 'refresh_default_identity', AsyncMock())
    monkeypatch.setattr(account_controls, '_spawn', AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(account_controls, '_hold', AsyncMock())
    monkeypatch.setattr(account_controls, '_run', AsyncMock(return_value=(0, b'')))
    result = await account_controls.start_signin(account['id'])
    assert result['status'] == 'waiting'
    assert get_account(account['id'], db=db)['state'] == 'signed_out'
    await account_controls._SIGNINS[account['id']].task
    account_controls._SIGNINS.pop(account['id'])
