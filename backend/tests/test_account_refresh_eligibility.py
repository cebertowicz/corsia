"""An ineligible refresh is never reported as a failure."""
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize(('provider', 'receipt', 'current_c6'), [
    ('github', object(), False),
    ('codex', None, False),
    ('codex', object(), True),
])
async def test_ineligible_refresh_does_not_run(monkeypatch, provider, receipt, current_c6):
    from connections import account_verify_refresh, harness_models

    refresh = AsyncMock()
    monkeypatch.setattr(harness_models, 'refresh_provider', refresh)
    assert await account_verify_refresh.refresh_models(
        {'provider': provider, 'id': 'account'}, receipt, '/synthetic', current_c6,
    ) == (None, None, None)
    refresh.assert_not_awaited()
