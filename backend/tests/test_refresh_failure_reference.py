"""Model-refresh exceptions retain a reference without exposing their details."""
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_refresh_exception_is_reported_with_safe_cause(monkeypatch, caplog):
    from connections import account_verify_refresh, harness_models

    monkeypatch.setattr(harness_models, 'refresh_provider', AsyncMock(side_effect=TimeoutError('private detail')))
    ok, cause, ref = await account_verify_refresh.refresh_models(
        {'provider': 'codex', 'id': 'account'}, object(), '/synthetic', False,
    )
    assert ok is False and cause == 'account_refresh_failed' and ref
    assert f'reference={ref} cause=TimeoutError' in caplog.text
    assert 'private detail' not in caplog.text
