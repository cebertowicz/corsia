"""The existing verify route reports the model refresh it already performs."""
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize(('refresh', 'expected'), [
    ((True, None, None), (True, None, None)),
    ((False, 'refresh_failed', 'REF123'), (False, 'refresh_failed', 'REF123')),
])
async def test_verify_route_reports_refresh_result(monkeypatch, refresh, expected):
    from api.vendor_accounts import verify_vendor_account
    from connections import account_controls

    account = {'id': 'a', 'provider': 'github', 'state': 'signed_in', 'source_home': '/fixture', 'revision': 1}
    monkeypatch.setattr(account_controls, 'custom_account', AsyncMock(return_value=account))
    monkeypatch.setattr(account_controls, 'account_receipt', lambda _: object())
    monkeypatch.setattr(account_controls, 'readiness_reason', lambda _: None)
    monkeypatch.setattr(account_controls, '_binary', AsyncMock(return_value='/fixture/cli'))
    monkeypatch.setattr(account_controls, '_vendor_status', AsyncMock(return_value=(True, {}, None)))
    monkeypatch.setattr(account_controls, 'refresh_models', AsyncMock(return_value=refresh))

    result = await verify_vendor_account('a')
    assert result['vendor_signed_in'] is True
    assert (result['model_refresh_ok'], result['model_refresh_cause'], result['model_refresh_ref']) == expected
