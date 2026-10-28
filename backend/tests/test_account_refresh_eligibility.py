"""A model refresh is reported only when it was eligible to run."""
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,receipt,current_c6", [
    ("github", object(), False),
    ("codex", None, False),
    ("codex", object(), True),
])
async def test_ineligible_refresh_is_not_a_failure(monkeypatch, caplog, provider, receipt, current_c6):
    from connections import account_verify_refresh, harness_models

    refresh = AsyncMock()
    monkeypatch.setattr(harness_models, "refresh_provider", refresh)
    result = await account_verify_refresh.refresh_models(
        {"provider": provider, "id": "account"}, receipt, "/synthetic", current_c6,
    )
    assert result == (None, None, None)
    refresh.assert_not_awaited()
    assert "account model refresh failed" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,receipt,c6", [
    ("github", object(), None),
    ("codex", None, None),
    ("codex", object(), "codex_auth_projection_changed"),
])
async def test_verify_reports_no_refresh_for_ineligible_account(monkeypatch, caplog, provider, receipt, c6):
    from connections import account_controls, harness_models

    account = {"id": "account", "provider": provider, "state": "signed_in", "source_home": "/synthetic"}
    monkeypatch.setattr(account_controls, "_custom", AsyncMock(return_value=account))
    monkeypatch.setattr(account_controls, "account_receipt", lambda _: receipt)
    monkeypatch.setattr(account_controls, "readiness_reason", lambda _: c6)
    monkeypatch.setattr(account_controls, "_binary", AsyncMock(return_value="/synthetic"))
    monkeypatch.setattr(account_controls, "_vendor_status", AsyncMock(return_value=(True, {}, None)))
    monkeypatch.setattr(account_controls, "refresh_default_identity", AsyncMock())
    refresh = AsyncMock()
    monkeypatch.setattr(harness_models, "refresh_provider", refresh)
    result = await account_controls.verify("account")
    assert (result["model_refresh_ok"], result["model_refresh_cause"], result["model_refresh_ref"]) == (None, None, None)
    refresh.assert_not_awaited()
    assert "account model refresh failed" not in caplog.text
    with pytest.raises(account_controls.AccountError, match="account is already signed in"):
        await account_controls.start_signin("account")
