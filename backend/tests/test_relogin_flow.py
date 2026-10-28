from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vendor_account_fixtures import accounts  # noqa: F401


@pytest.mark.asyncio
async def test_signed_in_account_confirmed_signed_out_continues_to_login(accounts, monkeypatch):
    from connections import account_controls
    from connections.vendor_accounts import get_account

    db, _, row, _ = accounts
    monkeypatch.setattr(account_controls, '_binary', AsyncMock(return_value='/synthetic'))
    monkeypatch.setattr(account_controls, '_vendor_status', AsyncMock(return_value=(False, {}, None)))
    monkeypatch.setattr(account_controls, 'refresh_default_identity', AsyncMock())
    spawn = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(account_controls, '_spawn', spawn)
    hold = AsyncMock()
    monkeypatch.setattr(account_controls, '_hold', hold)
    monkeypatch.setattr(account_controls, '_run', AsyncMock(return_value=(0, b'')))
    result = await account_controls.start_signin(row['id'])
    assert result['status'] == 'waiting'
    assert spawn.await_args.args[-1] == 'login'
    assert get_account(row['id'], db=db)['state'] == 'signed_out'
    assert account_controls._SIGNINS[row['id']].revision == row['revision'] + 1
    await account_controls._SIGNINS[row['id']].task
    account_controls._SIGNINS.pop(row['id'])
