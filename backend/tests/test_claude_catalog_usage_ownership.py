"""A catalog sign-in owns its co-read usage before capacity becomes visible."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from test_provider_sources import _err, _listed, _ok, _put, _stream, claude_session, client  # noqa: F401
from vendor_account_fixtures import default_id, default_receipt

pytestmark = [pytest.mark.asyncio, pytest.mark.real_harness_cli,
              pytest.mark.usefixtures("signed_in_defaults")]  # a catalog refresh reads a signed-in account


def _report(percent):
    reset = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
    return {"rate_limits_available": True, "rate_limits": {
        "five_hour": {"utilization": percent, "resets_at": reset},
        "seven_day_sonnet": {"utilization": 10, "resets_at": reset},
    }}


def _seed_old_account(status="needs_auth", provider="claude-code"):
    from connections import harness_usage

    _put(provider, status)
    reset = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    harness_usage.record_usage(provider=provider, windows=[{
        "vendor": provider, "kind": "five_hour" if provider == "claude-code" else "codex_primary",
        "used_percent": 100, "resets_at": reset, "source": "discovery",
    }], verdict={"state": "limited", "reason": "old_account", "resets_at": reset}, receipt=default_receipt(provider))
    if provider == "claude-code":
        harness_usage.record_limited(provider=provider, scope_model="sonnet", source="rate_limit_event",
                                     reason="old_scoped_limit", resets_at=reset, receipt=default_receipt(provider))


@pytest.fixture
async def live_app_loop(monkeypatch):
    from infra import app_loop

    monkeypatch.setattr(app_loop, "_LOOP", None)
    app_loop.capture(asyncio.get_running_loop())
    try:
        yield
    finally:
        app_loop.clear()


def _capacity():
    from connections import harness_usage

    return harness_usage.project_for_model(harness_usage.stored_usage(provider="claude-code", account_id=default_id("claude-code")),
                                          model="claude-code/sonnet")


@pytest.mark.parametrize("rediscover", [True, False], ids=["recheck", "startup"])
@pytest.mark.parametrize("reading", ["allowed", "limited", "missing", "failed", "write_failed"])
async def test_catalog_sign_in_owns_new_usage_before_commit(
    isolated_db, client, claude_session, monkeypatch, live_app_loop, rediscover, reading,
):
    from connections import claude_usage, harness_usage_check
    from connections.source_state import check_harness_source

    _seed_old_account()
    if reading in {"allowed", "limited"}:
        answer = _listed(usage=_report(20 if reading == "allowed" else 100))
    elif reading == "failed":
        answer = _stream(_ok("corsia-model-list", {"models": []}), _err("corsia-usage", "unreadable"))
    else:
        answer = _stream(_ok("corsia-model-list", {"models": []}))
    sessions = claude_session(answer)
    if reading == "write_failed":
        apply_usage = claude_usage.apply_usage_response

        def failed_write(_response, *, receipt, db=None):
            apply_usage(_report(20), receipt=receipt, db=db)
            raise OSError("usage write failed after updating the meter")

        monkeypatch.setattr(claude_usage, "apply_usage_response", failed_write)

    # Observe actual committed state through the canonical projection, including
    # the callbacks that would resume held inputs. No intermediate allowance may escape.
    committed = []
    monkeypatch.setattr("infra.state.ws.schedule_models_changed", lambda: committed.append(_capacity()))
    monkeypatch.setattr("infra.connection_limit_drive.schedule_scope_release",
                        lambda _subject: committed.append(_capacity()))
    scheduled = Mock(wraps=harness_usage_check.schedule_account_check)
    monkeypatch.setattr(harness_usage_check, "schedule_account_check", scheduled)

    await check_harness_source("claude-code", rediscover=rediscover)
    await asyncio.sleep(0)
    tasks = list(harness_usage_check._CHECKS.values())
    if tasks:
        await asyncio.gather(*tasks)


async def test_unreadable_refresh_preserves_the_same_accounts_known_limit(
    isolated_db, client, claude_session, live_app_loop,
):
    from connections.source_state import check_harness_source

    _seed_old_account(status="available")
    sessions = claude_session(_stream(_ok("corsia-model-list", {"models": []})))
    await check_harness_source("claude-code", rediscover=True)
    assert sessions == ["list_models"]
    assert _capacity()["state"] == "limited"
    assert _capacity()["check"]["outcome"] == "unknown"


