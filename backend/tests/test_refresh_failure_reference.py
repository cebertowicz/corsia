"""Refresh exceptions remain in diagnostics, never the account-row cause."""
import logging
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_refresh_exception_logs_class_and_returns_stable_cause(monkeypatch, caplog):
    from connections import account_verify_refresh, harness_models

    monkeypatch.setattr(harness_models, "refresh_provider", AsyncMock(side_effect=TimeoutError("private detail")))
    with caplog.at_level(logging.WARNING):
        ok, cause, ref = await account_verify_refresh.refresh_models(
            {"provider": "codex", "id": "account"}, object(), "/synthetic", False,
        )
    assert ok is False and cause == "account_refresh_failed" and ref
    assert f"reference={ref} cause=TimeoutError" in caplog.text
    assert "private detail" not in caplog.text
