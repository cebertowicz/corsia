"""A catalog sign-in owns its co-read usage before capacity becomes visible."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from test_provider_sources import _err, _listed, _ok, _put, _stream, claude_session, client  # noqa: F401
from vendor_account_fixtures import default_id, default_receipt

pytestmark = [pytest.mark.asyncio, pytest.mark.real_harness_cli]


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

        def failed_write(_response, *, db=None):
            apply_usage(_report(20), db=db)
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

    assert sessions == ["list_models"]
    scheduled.assert_not_called()
    capacity = _capacity()
    expected = reading if reading in {"allowed", "limited"} else "unknown"
    assert capacity["state"] == expected
    assert committed and {item["state"] for item in committed} == {expected}
    if reading in {"allowed", "limited"}:
        readings = {item["kind"]: item["used_percent"] for item in capacity["windows"]}
        assert readings == {"five_hour": 20 if reading == "allowed" else 100, "seven_day_sonnet": 10}
        assert capacity["usable"] is (reading == "allowed")
    else:
        assert capacity["check"]["outcome"] == "unknown"
        assert capacity["check"]["error"]
        assert all(item.get("used_percent") is None for item in capacity["windows"])


async def test_first_identity_observation_keeps_its_catalog_usage_fresh(
    isolated_db, client, claude_session, monkeypatch, live_app_loop,
):
    from connections.account_evidence import discovery_record
    from connections.vendor_accounts import default_account
    from connections import harness_usage
    from connections.source_state import check_harness_source

    _put("claude-code", "needs_auth")
    claude_session(_listed(usage=_report(20)))

    observed = []

    async def identity(_provider):
        observed.append(_provider)
        return {
            "email": "person@example.test", "org_id": "org-1",
            "org_name": "person@example.test's Organization", "plan": "Max",
        }

    monkeypatch.setattr("connections.account_identity.default_identity", identity)
    await check_harness_source("claude-code", rediscover=True)

    assert observed == ["claude-code"]
    account = default_account("claude-code")
    record = account["identity"]["default_comparison"]
    assert record["facts"] == {
        "email": "person@example.test", "org_id": "org-1",
        "org_name": "person@example.test's Organization", "plan": "Max",
    }
    usage = harness_usage.stored_usage(provider="claude-code", account_id=account["id"])
    assert harness_usage.project_for_model(usage, model="claude-code/sonnet")["state"] == "allowed"
    assert {window["kind"]: window["used_percent"] for window in usage["windows"].values()} == {
        "five_hour": 20, "seven_day_sonnet": 10,
    }
    assert discovery_record(account)["discovery"]["status"] == "unknown"


@pytest.mark.parametrize(("provider", "first", "second"), [
    ("claude-code", {"email": "person@example.test", "org_id": "org-1", "plan": "Pro"},
     {"email": "person@example.test", "org_id": "org-1", "plan": "Max"}),
    ("codex", {"account_fp": "person", "plan": "Free"},
     {"account_fp": "person", "plan": "Pro"}),
])
async def test_display_only_identity_facts_preserve_default_usage(
    isolated_db, client, live_app_loop, provider, first, second,
):
    from connections import harness_usage
    from connections.account_evidence import discovery_record
    from connections.account_observation import begin_default_observation, commit_default_observation
    from connections.vendor_accounts import default_account
    from infra.database import get_db

    _put(provider, "needs_auth")
    db = get_db()
    initial = commit_default_observation(
        provider, first, token=begin_default_observation(provider, db=db), schedule_usage_check=False, db=db,
    )
    assert initial.accepted and initial.login_changed
    account = default_account(provider, db=db)
    harness_usage.record_usage(
        provider=provider,
        windows=[{"vendor": provider, "kind": "five_hour", "used_percent": 20, "source": "discovery"}],
        verdict={"state": "allowed"}, receipt=default_receipt(provider, db=db), db=db,
    )
    before_usage = harness_usage.stored_usage(provider=provider, account_id=account["id"], db=db)
    before_login_seq = discovery_record(account)["login_seq"]

    observed = commit_default_observation(
        provider, second, token=begin_default_observation(provider, db=db), schedule_usage_check=False, db=db,
    )

    account = default_account(provider, db=db)
    assert observed.accepted and not observed.login_changed
    assert discovery_record(account)["login_seq"] == before_login_seq
    assert account["identity"]["default_comparison"]["facts"]["plan"] == second["plan"]
    assert harness_usage.stored_usage(provider=provider, account_id=account["id"], db=db) == before_usage
    db.close()


async def test_an_unreadable_identity_is_not_a_login_change(
    isolated_db, client, claude_session, monkeypatch, live_app_loop,
):
    from connections.account_evidence import discovery_record
    from connections.vendor_accounts import default_account
    from connections import harness_usage
    from connections.source_state import check_harness_source

    _put("claude-code", "needs_auth")
    readings = [{"email": "known@example.test", "org_id": "org-1"}, {}]

    async def identity(_provider):
        return readings.pop(0)

    monkeypatch.setattr("connections.account_identity.default_identity", identity)
    claude_session(_listed(usage=_report(20)))
    await check_harness_source("claude-code", rediscover=True)
    known = default_account("claude-code")
    claude_session(_listed(usage=_report(30)))
    await check_harness_source("claude-code", rediscover=True)

    assert readings == []
    account = default_account("claude-code")
    assert account["identity"] == known["identity"]
    assert account["revision"] == known["revision"]
    assert discovery_record(account)["login_seq"] == discovery_record(known)["login_seq"]
    usage = harness_usage.stored_usage(provider="claude-code", account_id=account["id"])
    assert harness_usage.project_for_model(usage, model="claude-code/sonnet")["state"] == "allowed"
    assert {window["kind"]: window["used_percent"] for window in usage["windows"].values()} == {
        "five_hour": 30, "seven_day_sonnet": 10,
    }


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


@pytest.mark.parametrize("provider", ["claude-code", "codex"])
async def test_ordinary_sign_in_still_reads_the_new_account_once(
    isolated_db, client, monkeypatch, live_app_loop, provider,
):
    from connections import harness_usage, harness_usage_check
    from connections.models import set_connection_status
    from infra.db_async import run_in_db

    _seed_old_account(provider=provider)
    read = asyncio.Event()
    binaries = []

    async def usage_read(binary, receipt):
        assert receipt.account_id == default_id(provider)
        binaries.append(binary)
        read.set()

    monkeypatch.setattr(harness_usage_check, "_check_claude" if provider == "claude-code" else "_check_codex", usage_read)
    await run_in_db(set_connection_status, provider, "available")
    await asyncio.wait_for(read.wait(), timeout=5)
    await asyncio.gather(*list(harness_usage_check._CHECKS.values()))
    assert binaries == [f"/bin/{provider}"]
    usage = harness_usage.stored_usage(provider=provider, account_id=default_id(provider))
    assert all(window.get("used_percent") is None for window in usage["windows"].values())
    assert harness_usage.project_for_model(usage)["state"] == "allowed"
