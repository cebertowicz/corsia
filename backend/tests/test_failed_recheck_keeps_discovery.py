"""An added-account usage recheck preserves catalog and receipt-fenced limit state.

Only the bounded Claude control process is faked here; the recheck and usage persistence
paths are real."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from test_provider_sources import _err, _ok, _stream
from vendor_account_fixtures import MODEL, accounts, claude  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.real_harness_cli]

RESET = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()


def _listed_with(usage_frame):
    return _stream(_ok("corsia-model-list", {"models": [{"value": "test", "resolvedModel": "test-1"}]}), usage_frame)


@pytest.fixture
def limited_added_account(claude, monkeypatch):
    from connections import claude_discovery, harness_models, harness_usage
    from connections.account_evidence import mark_discovery
    from connections.account_receipts import capture_receipt

    db, account_id = claude.db, claude.other
    receipt = capture_receipt("claude-code", account_id=account_id, db=db)
    assert mark_discovery(account_id, receipt=receipt, status="ready",
                          models={MODEL: {"available": True, "resolved_identity": "test-1"}}, db=db)
    db.commit()
    harness_usage.record_limited(provider="claude-code", receipt=receipt, reason="five_hour_limit",
                                 resets_at=RESET, source="discovery")

    async def version(_binary, **_kwargs):
        return "1.0.0"

    async def context(_binary, _selector, **_kwargs):
        return {"rawMaxTokens": 200000}

    monkeypatch.setattr(harness_models, "binary_version", version)
    monkeypatch.setattr(claude_discovery, "_discover_claude_context", context)
    monkeypatch.setattr(harness_models, "_ADAPTERS", {**harness_models._ADAPTERS, "claude-code": (
        claude_discovery.discover_claude_models, *harness_models._ADAPTERS["claude-code"][1:])})
    return claude


async def _recheck(claude, monkeypatch, outcome):
    from connections import claude_discovery
    from connections import harness_usage_check as checks

    async def control(_binary, _frames, **_kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def binary(_provider):
        return "/synthetic/claude"

    monkeypatch.setattr(claude_discovery, "run_claude_control", control)
    monkeypatch.setattr(checks, "_binary_for", binary)
    checks._LAST_STARTED.clear()
    await checks._check("claude-code", claude.other)


def _read(claude):
    from connections import harness_usage
    from connections.account_evidence import readiness_reason
    from connections.vendor_accounts import get_account

    account = get_account(claude.other, db=claude.db)
    usage = harness_usage.stored_usage(provider="claude-code", account_id=claude.other, db=claude.db)
    projection = harness_usage.project_for_model(usage, model=MODEL, provider="claude-code")
    return readiness_reason(account, model=MODEL), projection, (usage or {}).get("last_check")


@pytest.mark.parametrize("outcome", [
    RuntimeError("Claude Code list_models timed out"),
    RuntimeError("Claude Code list_models failed: exited 1"),
    b"",
], ids=("timeout", "nonzero-exit", "no-reply"))
async def test_a_recheck_that_cannot_finish_keeps_the_models_and_the_last_verdict(
        limited_added_account, monkeypatch, outcome):
    claude = limited_added_account
    await _recheck(claude, monkeypatch, outcome)

    reason, projection, last_check = _read(claude)
    assert reason is None
    assert projection["usable"] is False
    assert projection["resets_at"] == RESET
    assert last_check and last_check["outcome"] == "unknown"
    if isinstance(outcome, BaseException):
        assert last_check["error"] == f"claude-code usage read failed: {outcome}"


async def test_a_usage_read_that_fails_alone_keeps_the_models(limited_added_account, monkeypatch):
    claude = limited_added_account
    await _recheck(claude, monkeypatch, _listed_with(_err("corsia-usage", "usage unavailable")))

    reason, _projection, last_check = _read(claude)
    assert reason is None
    assert last_check and last_check["outcome"] == "unknown"


async def test_an_added_claude_recheck_uses_its_scoped_usage_read_without_discovery(limited_added_account, monkeypatch):
    from connections import claude_discovery, harness_models
    from connections import harness_usage_check as checks
    from connections.account_receipts import capture_receipt
    from connections.vendor_accounts import get_account

    claude = limited_added_account
    account = get_account(claude.other, db=claude.db)
    receipt = capture_receipt("claude-code", account_id=claude.other, db=claude.db)
    calls = []

    async def control(binary, frames, **kwargs):
        calls.append((binary, frames, kwargs))
        return _stream(_ok("corsia-usage", {
            "rate_limits_available": True,
            "rate_limits": {"five_hour": {"utilization": 100, "resets_at": RESET}},
        }))

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("a usage recheck must not refresh discovery")

    monkeypatch.setattr(claude_discovery, "run_claude_control", control)
    monkeypatch.setattr(harness_models, "refresh_provider", forbidden)
    monkeypatch.setattr(checks, "_binary_for", lambda _provider: asyncio.sleep(0, result="/synthetic/claude"))
    checks._LAST_STARTED.clear()

    await checks._check("claude-code", claude.other)

    assert len(calls) == 1
    binary, frames, kwargs = calls[0]
    assert binary == "/synthetic/claude"
    assert [frame["request_id"] for frame in frames] == ["corsia-usage", "corsia-usage-end"]
    assert kwargs["operation"] == "get_usage"
    assert kwargs["context"].account_id == claude.other
    assert kwargs["context"].source_home == account["source_home"]
    assert kwargs["context"].receipt == receipt
    reason, projection, last_check = _read(claude)
    assert reason is None and projection["usable"] is False
    assert last_check and last_check["outcome"] == "limited"


async def test_stale_added_claude_usage_receipt_changes_nothing(limited_added_account, monkeypatch):
    from connections import claude_discovery, harness_usage, harness_usage_check as checks
    from connections.account_evidence import discovery_record, invalidate_discovery
    from connections.account_receipts import capture_receipt
    from connections.vendor_accounts import get_account

    claude = limited_added_account
    account = get_account(claude.other, db=claude.db)
    receipt = capture_receipt("claude-code", account_id=claude.other, db=claude.db)
    before_usage = harness_usage.stored_usage(
        provider="claude-code", account_id=claude.other, db=claude.db,
    )

    async def control(_binary, _frames, **_kwargs):
        current = get_account(claude.other, db=claude.db)
        next_login = discovery_record(current)["login_seq"] + 1
        invalidate_discovery(current, login_seq=next_login, auth_state="known", db=claude.db)
        claude.db.commit()
        return _stream(_ok("corsia-usage", {
            "rate_limits_available": True,
            "rate_limits": {"five_hour": {"utilization": 1}},
        }))

    monkeypatch.setattr(claude_discovery, "run_claude_control", control)
    monkeypatch.setattr(checks, "_binary_for", lambda _provider: asyncio.sleep(0, result="/synthetic/claude"))
    checks._LAST_STARTED.clear()
    await checks._check("claude-code", claude.other)

    assert capture_receipt("claude-code", account_id=claude.other, db=claude.db) != receipt
    assert harness_usage.stored_usage(provider="claude-code", account_id=claude.other, db=claude.db) == before_usage


async def test_only_the_same_logins_last_good_discovery_survives_a_failed_run(claude):
    from connections.account_evidence import discovery_record, mark_discovery, readiness_reason
    from connections.account_receipts import capture_receipt
    from connections.vendor_accounts import get_account

    db, account_id = claude.db, claude.other
    receipt = capture_receipt("claude-code", account_id=account_id, db=db)

    def state():
        account = get_account(account_id, db=db)
        return discovery_record(account)["discovery"], readiness_reason(account, model=MODEL)

    # A first run that fails has no good discovery to keep.
    assert mark_discovery(account_id, receipt=receipt, status="failed", cause="account_discovery_failed", db=db)
    assert state()[0]["status"] == "failed" and state()[1] == "account_model_not_discovered"

    # After a good run, a failed one keeps the models and records the failure beside them.
    assert mark_discovery(account_id, receipt=receipt, status="ready", models={MODEL: {"available": True}}, db=db)
    assert mark_discovery(account_id, receipt=receipt, status="failed", cause="account_discovery_failed", db=db)
    discovery, reason = state()
    assert reason is None and discovery["status"] == "ready"
    assert discovery["refresh_failure"]["cause"] == "account_discovery_failed"

    # The next good run replaces the record, failure note included.
    assert mark_discovery(account_id, receipt=receipt, status="ready", models={MODEL: {"available": True}}, db=db)
    assert "refresh_failure" not in state()[0]
