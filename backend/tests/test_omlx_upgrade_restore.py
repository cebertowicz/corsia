"""Upgrade completion requires the original models to be loaded, not just a returned reload call."""

import pytest

from llm.providers import omlx_upgrade
from test_omlx_upgrade import _seat


@pytest.mark.parametrize("after", [None, [], [{"identifier": "A/first-oQ4e-mtp"}], [{"identifier": "A/other-variant"}]])
async def test_reload_returning_without_restoring_the_models_is_not_success(isolated_db, monkeypatch, after):
    calls, _ = _seat(monkeypatch, recorded="0.7.1rc1")
    snapshots = iter([[{"identifier": "A/first-oQ4e-mtp"}, {"identifier": "B/second-mtp"}], after])

    async def read_loaded(_cfg):
        return next(snapshots)

    monkeypatch.setattr("llm.providers.omlx_lifecycle.read_loaded", read_loaded)
    result = await omlx_upgrade.upgrade_managed_omlx()

    assert result["restarted"] is True
    assert result["reason"] == "reload_failed"
    assert result["missing"]  # unreadable status also cannot confirm either original model
    assert ("reload", "seat", ["A/first-oQ4e-mtp", "B/second-mtp"]) in calls
