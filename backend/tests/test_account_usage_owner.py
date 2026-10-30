"""Usage has exactly one owner: the account row, never the connection config.

Pins: the account-usage foundation, the stored-usage version check, usage verdicts, one meter
per account, harness usage and its background check, usage receipts and a re-checked Default."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest
from subagents.codex_usage_observer import handle_rate_limits_notification, RATE_LIMITS_UPDATED

from connections import (
    claude_usage,
    harness_usage,
    harness_usage as codex_usage,
    harness_usage_check as checks,
    usage_store,
)
from connections.claude_usage import apply_rate_limit_event, usage_verdict
from connections.usage_verdicts import (
    ACCOUNT_SCOPE,
    effective_verdict,
    scope_key,
    scope_matches,
    window_scope,
)
from infra.database import get_db
from vendor_account_fixtures import (  # noqa: F401
    _accept,
    _held,
    _kind,
    _known_default,
    _ready,
    _row,
    accounts,
    claude,
    default_id,
    default_receipt,
    KEY,
    no_stops,
    notification_owner,
    ready,
    receipt_of,
)


pytestmark = pytest.mark.usefixtures('signed_in_defaults')  # a signed-in Default per harness: no ambient login


def _seed_connection_row(provider):
    from connections.models import upsert_connection
    return upsert_connection(type="cli", provider=provider, status="available", config={"binary": "/bin/test"})


def _report(limits: object, *, available: bool = True) -> dict:
    return {"rate_limits_available": available, "rate_limits": limits}


def test_unknown_claude_usage_logs_only_fixed_shape_categories(monkeypatch, caplog):
    recorded = []
    monkeypatch.setattr(harness_usage, "record_usage", lambda **kwargs: recorded.append(kwargs))

    claude_usage.apply_usage_response({
        "rate_limits_available": "private-non-boolean",
        "rate_limits": "private-malformed-limits",
        "account_id": "private-account-id",
    }, receipt=None)

    messages = [record.getMessage() for record in caplog.records
                if record.name == "connections.claude_usage"]
    assert messages == [
        "Claude Code usage unknown rate_limits_type=string rate_limits_available=present",
    ]
    assert recorded and recorded[0]["check_error"] == "Claude Code returned malformed plan usage"


async def drain():
    """Wait for every wake already scheduled, and every wake those schedule, to finish.

    A cleared limit wakes a held head by two routes, catalog recovery and the
    connection-limit release, each its own tracked task, and whichever claims the
    head carries its delivery. Waiting on one route, or counting yields, misses the other.
    """
    from infra import unavailable_model_recovery as recovery
    from ws.ingress import background_tasks

    loop = asyncio.get_running_loop()
    await asyncio.sleep(0)  # An on-loop app_loop.submit starts its task on the next iteration.
    while pending := {task for task in background_tasks if task.get_loop() is loop and not task.done()}:
        await asyncio.wait_for(asyncio.wait(pending), 5)
    assert recovery._TASK is not None


class FakeControl:
    mutation = None
    instances: list['FakeControl'] = []

    def __init__(self, **kwargs):
        assert kwargs == {'binary': '/bin/test', 'task': None, 'control_only': True}
        self.closed = False
        self.instances.append(self)

    async def start(self):
        pass

    async def close(self, **kwargs):
        self.closed = True

    async def _request(self, method, params):
        assert (method, params) == ('account/rateLimits/read', None)
        if self.mutation:
            self.mutation()
        return {'ordinaryUsageAllowed': True, 'rateLimits': {}}


@pytest.fixture
def control(monkeypatch):
    from connections import harness_usage_check as checks

    FakeControl.instances = []
    FakeControl.mutation = None
    monkeypatch.setattr(checks, '_CHECKS', {})
    monkeypatch.setattr(checks, '_binary_for', lambda _p: asyncio.sleep(0, result='/bin/test'))
    monkeypatch.setattr('subagents.codex_app_server.CodexAppServerSession', FakeControl)
    yield FakeControl
    FakeControl.mutation = None


def _seed(provider: str, **config) -> None:
    from connections.models import ConnectionStatus, ConnectionType, upsert_connection

    upsert_connection(type=ConnectionType.CLI, provider=provider,
                      status=ConnectionStatus.AVAILABLE, config={"binary": "/bin/test", **config})


def _db():
    from infra.database import get_db

    return get_db()


def _read_full_window_on_credits(db) -> dict:
    later = int((datetime.now(timezone.utc) + timedelta(hours=2)).timestamp())
    harness_usage.record_rate_limits_read({
        "ordinaryUsageAllowed": True,
        "rateLimits": {"primary": {"usedPercent": 100, "resetsAt": later}, "credits": {"hasCredits": True}},
    }, provider="codex", db=db, receipt=default_receipt("codex", db=db))
    return harness_usage.project_for_model(harness_usage.stored_usage(provider="codex", db=db, account_id=default_id("codex", db=db)))


@pytest.fixture
def _checks_clean(monkeypatch, isolated_db):
    monkeypatch.setattr(checks, "_CHECKS", {})
    monkeypatch.setattr(checks, "_LAST_STARTED", {})
    monkeypatch.setattr(checks, "_NEW_LOGIN_PENDING", set())


def _limited(provider: str) -> None:
    codex_usage.record_limited(reason="limit", resets_at=None, source="test", provider=provider, receipt=default_receipt(provider))


class _Process:
    returncode = 0

    def __init__(self, response: dict | None = None, *, error: str = ""):
        self.response, self.error, self.killed = response, error, False
        self.stdin = self.stdout = self._transport = self
        self._stdout_read = False

    def write(self, _payload):
        pass

    async def drain(self):
        pass

    def close(self):
        pass

    def get_pipe_transport(self, _fd):
        return self

    async def read(self, _limit=-1):
        if self.error == "timeout":
            raise asyncio.TimeoutError
        if self._stdout_read:
            return b""
        self._stdout_read = True
        return b"" if self.response is None else (json.dumps(self.response) + "\n").encode()

    async def _read_stream(self, _fd):
        return self.error.encode()

    async def wait(self):
        return self.returncode

    async def communicate(self, _payload: bytes = b""):
        if self.error == "timeout":
            raise asyncio.TimeoutError
        stdout = b"" if self.response is None else (json.dumps(self.response) + "\n").encode()
        return stdout, self.error.encode()

    def kill(self):
        self.killed = True


def _usage_response(report: dict) -> dict:
    return {"response": {"subtype": "success", "request_id": "corsia-usage", "response": report}}


WEEK = 10080


def _seed_meter_connection(provider: str, **config) -> None:
    from connections.models import ConnectionStatus, ConnectionType, upsert_connection

    supplied_usage = config.pop("usage", None)
    upsert_connection(type=ConnectionType.CLI, provider=provider,
                      status=ConnectionStatus.AVAILABLE, config={"binary": "/bin/test", **config})
    if provider == "codex":
        from vendor_account_fixtures import record_default_identity

        db = _db()
        try:
            record_default_identity("codex", {"account_fp": "synthetic-one-meter-default"}, db=db)
        finally:
            db.close()
    if supplied_usage is not None:
        from connections.vendor_accounts import resolve_account_id

        db = _db()
        try:
            identity = resolve_account_id(provider, db=db)
            db.execute("UPDATE vendor_accounts SET usage_json=? WHERE id=?", (json.dumps(supplied_usage), identity))
            db.commit()
            actual_config = db.execute("SELECT config FROM connections WHERE provider=?", (provider,)).fetchone()[0]
            assert "usage" not in json.loads(actual_config or "{}")
        finally:
            db.close()


def _reset() -> int:
    return int((datetime.now(timezone.utc) + timedelta(days=6)).timestamp())


def _snapshot(limit_id: str | None, primary: int, *, secondary: tuple[int, int] | None = None) -> dict:
    snapshot: dict = {
        "limitId": limit_id, "limitName": None,
        "primary": {"usedPercent": primary, "windowDurationMins": WEEK, "resetsAt": _reset()},
        "secondary": None,
    }
    if secondary:
        snapshot["secondary"] = {"usedPercent": secondary[0], "windowDurationMins": secondary[1], "resetsAt": _reset()}
    return snapshot


def _read(main: dict, buckets: dict[str, dict], db) -> None:
    harness_usage.record_rate_limits_read(
        {"ordinaryUsageAllowed": True, "rateLimits": main, "rateLimitsByLimitId": buckets},
        provider="codex", db=db, receipt=default_receipt("codex", db=db),
    )


def _live_update(update: dict) -> None:
    from connections.account_receipts import capture_receipt
    from connections.vendor_accounts import resolve_account_id

    db = _db()
    try:
        identity = resolve_account_id("codex", db=db)
        receipt = capture_receipt("codex", account_id=identity, db=db)
    finally:
        db.close()
    session = SimpleNamespace(_rate_limits=None, vendor_account_bound=True,
                              vendor_account_id=identity, vendor_account_receipt=receipt)
    assert asyncio.run(handle_rate_limits_notification(session, RATE_LIMITS_UPDATED, {"rateLimits": update}))


def _stored(db) -> dict:
    return harness_usage.stored_usage(provider="codex", db=db, account_id=default_id("codex", db=db))["windows"]


def _shown(db, model: str | None = None) -> list[tuple[str, int]]:
    projection = harness_usage.project_for_model(harness_usage.stored_usage(provider="codex", db=db, account_id=default_id("codex", db=db)), model=model)
    return [(window["kind"], window["used_percent"]) for window in projection["windows"]]


def _version_1_blob() -> dict:
    reset = (datetime.now(timezone.utc) + timedelta(days=6)).isoformat()
    observed = datetime.now(timezone.utc).isoformat()
    window = {"vendor": "codex", "kind": "codex_primary", "window_minutes": WEEK, "resets_at": reset}
    return {"version": 1, "facts": {}, "windows": {
        "codex:codex_primary:account": {**window, "used_percent": 7, "observed_at": observed, "source": "rate_limit_update"},
        "codex:codex:account": {**window, "limit_id": "codex", "used_percent": 6, "observed_at": observed, "source": "discovery"},
    }}


def _claude_windows() -> dict:
    reset = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    observed = datetime.now(timezone.utc).isoformat()
    base = {"vendor": "claude-code", "resets_at": reset, "observed_at": observed, "source": "discovery"}
    return {"version": usage_store.USAGE_VERSION, "windows": {
        "claude-code:five_hour:account": {**base, "kind": "five_hour", "used_percent": 12},
        "claude-code:seven_day:account": {**base, "kind": "seven_day", "used_percent": 45},
        # `get_usage` may report a tier window with no scope of its own.
        "claude-code:seven_day_sonnet:account": {**base, "kind": "seven_day_sonnet", "used_percent": 80},
        "claude-code:nimbus_quill:account": {**base, "kind": "nimbus_quill", "used_percent": 3},
    }}


_PROVIDER = "claude-code"


_RESET = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()


def _seed_stored_usage(db, usage: dict) -> None:
    from connections.vendor_accounts import resolve_account_id

    identity = resolve_account_id(_PROVIDER, db=db)
    db.execute("UPDATE vendor_accounts SET usage_json=? WHERE id=?", (json.dumps(usage), identity))
    db.commit()
    config = db.execute("SELECT config FROM connections WHERE provider=?", (_PROVIDER,)).fetchone()[0]
    assert "usage" not in json.loads(config or "{}")


def _raw_usage(db) -> dict:
    from connections.vendor_accounts import resolve_account_id

    identity = resolve_account_id(_PROVIDER, db=db)
    row = db.execute("SELECT usage_json FROM vendor_accounts WHERE id=?", (identity,)).fetchone()
    return json.loads(row["usage_json"])


def _v1(windows: dict) -> dict:
    return {"version": 1, "windows": windows, "facts": {}}


_OLD_AT = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


# Version 1 stored the event's raw 0-1 fraction: 0.55 is 55% used, read as 99% left.
_V1_SONNET_EVENT = {
    "vendor": _PROVIDER, "kind": "seven_day_sonnet", "scope_model": "sonnet", "used_percent": 0.55,
    "used_percent_observed_at": _OLD_AT, "resets_at": _RESET, "observed_at": _OLD_AT, "source": "rate_limit_event",
}


_V1_FIVE_HOUR_DISCOVERY = {
    "vendor": _PROVIDER, "kind": "five_hour", "used_percent": 40.0, "resets_at": _RESET,
    "observed_at": _OLD_AT, "source": "discovery",
}


def _windows(usage: dict, kind: str) -> list[dict]:
    return [window for window in usage["windows"].values() if window.get("kind") == kind]


_OTHER_METER = {"vendor": "codex", "limit_id": "other", "kind": "codex_primary", "used_percent": 20, "observed_at": _OLD_AT}


NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


FUTURE = "2026-10-01T00:00:00+00:00"


PAST = "2026-09-30T00:00:00+00:00"


def _usage(**verdicts) -> dict:
    return {"vendor_verdicts": verdicts}


def test_migration_moves_exact_r1_usage_once_without_guessing_old_spend():
    from infra.migration_80_to_81_vendor_accounts import migrate_80_to_81
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE connections(id TEXT PRIMARY KEY, provider TEXT UNIQUE, config TEXT,
                                 created_at TEXT,updated_at TEXT);
        CREATE TABLE threads(id TEXT PRIMARY KEY,model TEXT);
        CREATE TABLE models(id TEXT PRIMARY KEY,litellm_model TEXT,harness TEXT);
        CREATE TABLE active_turns(id TEXT PRIMARY KEY);
        CREATE TABLE llm_usage(id TEXT PRIMARY KEY);
        INSERT INTO active_turns VALUES ('old-turn');
        INSERT INTO llm_usage VALUES ('old-spend');
        INSERT INTO threads VALUES ('legacy','claude-code/opus');
        INSERT INTO models VALUES ('claude-code/opus','claude-code/opus','claude-code');
        INSERT INTO models VALUES ('shadow','claude-code/opus','native');
        INSERT INTO threads VALUES ('unique-alias','alias-one');
        INSERT INTO models VALUES ('catalog-one','alias-one','claude-code');
        INSERT INTO threads VALUES ('ambiguous-alias','alias-two');
        INSERT INTO models VALUES ('catalog-two','alias-two','claude-code');
        INSERT INTO models VALUES ('catalog-three','alias-two','claude-code');
        INSERT INTO threads VALUES ('unknown','future-model');
    """)
    usage = {"version": 1, "windows": {"five": {"used_percent": 19}}, "facts": {"plan": "max"},
             "vendor_verdicts": {"null": {"state": "limited"}, "opus": {"state": "allowed"}},
             "last_check": {"outcome": "unknown", "error": "timeout"}}
    config = {"binary": "/bin/test", "usage": usage}
    db.execute("INSERT INTO connections VALUES ('c','claude-code',?,'t0','t0')", (json.dumps(config),))
    db.execute("INSERT INTO connections VALUES ('g','github','{}','t0','t0')")
    migrate_80_to_81(db)
    account_id, raw = db.execute("SELECT id,usage_json FROM vendor_accounts").fetchone()
    assert json.loads(raw) == usage
    assert json.loads(db.execute("SELECT config FROM connections WHERE id='c'").fetchone()[0]) == {"binary": "/bin/test"}
    assert db.execute("SELECT account_id FROM thread_vendor_accounts WHERE thread_id='legacy'").fetchone()[0] == account_id
    assert db.execute("SELECT account_id FROM thread_vendor_accounts WHERE thread_id='unique-alias'").fetchone()[0] == account_id
    assert db.execute("SELECT COUNT(*) FROM thread_vendor_accounts WHERE thread_id IN ('ambiguous-alias','unknown')").fetchone()[0] == 0
    assert db.execute("SELECT model FROM threads WHERE id='legacy'").fetchone()[0] == "claude-code/opus"
    assert db.execute("SELECT vendor_account_id FROM active_turns").fetchone()[0] is None
    assert db.execute("SELECT vendor_account_id FROM llm_usage").fetchone()[0] is None
    migrate_80_to_81(db)
    assert db.execute("SELECT COUNT(*) FROM vendor_accounts").fetchone()[0] == 1
    assert db.execute("SELECT id,usage_json FROM vendor_accounts").fetchone() == (account_id, raw)


@pytest.mark.parametrize("provider", ["claude-code", "codex"])
def test_account_usage_isolation_default_compatibility_and_no_dual_read(isolated_db, provider):
    from connections import harness_usage
    from connections.account_receipts import ReceiptFence
    from connections.vendor_accounts import add_account, default_account, set_account_state
    from infra.database import get_db
    _seed_connection_row(provider)
    default = default_account(provider)
    second = add_account(provider, "Work")
    second = set_account_state(second["id"], "signed_in", expected_revision=second["revision"])
    harness_usage.record_usage(provider=provider, verdict={"state": "limited", "reason": "account-limit"}, receipt=default_receipt(provider))
    harness_usage.record_usage(provider=provider, receipt=receipt_of(provider, second["id"]), facts={"plan": "different"},
                               verdict={"state": "allowed", "scope_model": "opus"})
    assert harness_usage.active_limit(provider=provider, account_id=default_id(provider))["reason"] == "account-limit"
    assert harness_usage.active_limit(provider=provider, account_id=second["id"]) is None
    assert harness_usage.stored_usage(provider=provider, account_id=second["id"])["facts"] == {"plan": "different"}
    db = get_db()
    assert "usage" not in json.loads(db.execute("SELECT config FROM connections WHERE provider=?", (provider,)).fetchone()[0])
    # An accidental legacy key cannot become a second truth after migration.
    db.execute("UPDATE connections SET config=? WHERE provider=?", (json.dumps({"usage": {"facts": {"plan": "wrong"}}}), provider))
    db.commit()
    assert harness_usage.stored_usage(provider=provider, account_id=second["id"])["facts"]["plan"] == "different"
    injected = db.execute("SELECT config FROM connections WHERE provider=?", (provider,)).fetchone()[0]
    harness_usage.record_allowed(provider=provider, receipt=receipt_of(provider, second["id"], db=db), source="paid-call", db=db)
    assert db.execute("SELECT config FROM connections WHERE provider=?", (provider,)).fetchone()[0] == injected
    assert harness_usage.active_limit(provider=provider, db=db, account_id=default_id(provider, db=db))["reason"] == "account-limit"
    other_provider = "codex" if provider == "claude-code" else "claude-code"
    _seed_connection_row(other_provider)
    wrong_vendor = default_account(other_provider)["id"]
    # A receipt naming no account of this vendor is refused with no effect.
    for invalid in (ReceiptFence("missing", 0, 0), receipt_of(other_provider, wrong_vendor)):
        harness_usage.record_usage(provider=provider, receipt=invalid, facts={"plan": "wrong"})
    assert harness_usage.stored_usage(provider=provider, account_id=default["id"])["facts"] == {}
    assert harness_usage.stored_usage(provider=other_provider, account_id=wrong_vendor) is None
    db.close()


def test_private_homes_and_identity_survive_lifecycle(isolated_db):
    from connections.vendor_accounts import (AccountError, account_homes, account_identity_key,
                                              add_account, default_account, rename_account, set_account_state)
    _seed_connection_row("codex")
    default_source, default_runtime = account_homes(default_account("codex")["id"])  # the Default has private homes too
    assert default_source != default_runtime
    account = add_account("codex", "Work")
    source, runtime = account_homes(account["id"])
    assert source != runtime
    assert all(path.stat().st_mode & 0o777 == 0o700 for path in (source, runtime))
    identity = account_identity_key(account["id"])
    renamed = rename_account(account["id"], "Renamed", expected_revision=account["revision"])
    removed = set_account_state(account["id"], "removed", expected_revision=renamed["revision"])
    assert account_identity_key(account["id"]) == identity
    assert account_homes(account["id"]) == (source, runtime)
    with pytest.raises(AccountError):
        set_account_state(account["id"], "signed_in", expected_revision=removed["revision"])


def test_account_metadata_cannot_read_usage_column(isolated_db):
    from connections.vendor_accounts import add_account, default_account, get_account, list_accounts
    from infra.database import get_db
    _seed_connection_row("codex")
    db = get_db()
    statements = []
    db.set_trace_callback(statements.append)
    account = add_account("codex", "Work", db=db)
    get_account(account["id"], db=db)
    default_account("codex", db=db)
    list_accounts(db=db)
    db.set_trace_callback(None)
    reads = [s.lower() for s in statements if s.lstrip().lower().startswith("select")]
    assert not any("usage_json" in s or "select *" in s for s in reads)
    assert "usage" not in account
    db.close()


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        (_report({"five_hour": {"utilization": 100, "resets_at": 42}}), "limited"),
        (_report({"five_hour": {"utilization": 99}}), "allowed"),
        (_report({"five_hour": {"utilization": 100}, "extra_usage": {"is_enabled": True}}), "unknown"),
        (_report({"five_hour": {"utilization": 100}, "extra_usage": {"is_enabled": True, "utilization": 20}}), "allowed"),
        (_report({"five_hour": {"utilization": 100}, "extra_usage": {"is_enabled": True, "utilization": 100}}), "limited"),
        (_report(None), "unknown"),
        (_report({}, available=False), "unknown"),
        (_report({"seven_day_opus": {"utilization": 100}}), "unknown"),
    ],
)
def test_usage_verdict_handles_vendor_windows(report, expected):
    assert usage_verdict(report)["verdict"] == expected


def test_usage_verdict_distinguishes_unavailable_malformed_and_absent_reports():
    unavailable = usage_verdict(_report({}, available=False))
    malformed = usage_verdict({"rate_limits_available": True, "rate_limits": None})
    absent = usage_verdict(None)
    assert unavailable["error"] == "Claude Code reports plan usage unavailable for this account"
    assert malformed["error"] == "Claude Code returned malformed plan usage"
    assert absent["error"] == "Claude Code returned no usage report"


def test_usage_verdict_keeps_latest_reset_when_known():
    verdict = usage_verdict(_report({"five_hour": {"utilization": 100, "resets_at": 42}}))
    assert verdict["resets_at"] == "1970-01-01T00:00:42+00:00"


def test_rate_limit_event_uses_vendor_rejection(monkeypatch):
    calls = []
    monkeypatch.setattr("connections.claude_usage.harness_usage.record_usage", lambda **kw: calls.append(kw))
    apply_rate_limit_event({"status": "rejected", "rateLimitType": "five_hour", "resetsAt": 42}, receipt=default_receipt('claude-code'))
    assert calls[0]["verdict"]["reason"] == "five_hour_limit"
    assert calls[0]["windows"][0]["kind"] == "five_hour"


def test_rejected_event_with_allowed_overage_stays_usable(monkeypatch):
    calls = []
    monkeypatch.setattr("connections.claude_usage.harness_usage.record_usage", lambda **kw: calls.append(kw))
    apply_rate_limit_event({"status": "rejected", "overageStatus": "allowed", "rateLimitType": "five_hour"}, receipt=default_receipt('claude-code'))
    assert calls[0]["verdict"]["state"] == "allowed"


@pytest.mark.parametrize(
    ("info", "inferred"),
    [
        ({"status": "rejected", "rateLimitType": "five_hour"}, 100),
        ({"status": "allowed_warning", "isUsingOverage": True, "rateLimitType": "seven_day_sonnet"}, 100),
        ({"status": "rejected", "rateLimitType": "overage"}, None),
        ({"status": "allowed", "isUsingOverage": True, "overageInUse": True, "rateLimitType": "overage"}, None),
        ({"status": "allowed", "rateLimitType": "five_hour"}, None),
        ({"status": "rejected", "rateLimitType": "five_hour", "utilization": 0.87}, 87),
    ],
)
def test_full_window_is_inferred_only_for_a_spent_plan_window(monkeypatch, info, inferred):
    """Inferred from vendor status, not read: never for a non-plan kind or over a reading."""
    calls = []
    monkeypatch.setattr("connections.claude_usage.harness_usage.record_usage", lambda **kw: calls.append(kw))
    apply_rate_limit_event(info, receipt=default_receipt('claude-code'))
    assert calls[0]["windows"][0]["used_percent"] == inferred


def test_rate_limit_event_live_source_is_preserved(monkeypatch):
    calls = []
    monkeypatch.setattr("connections.claude_usage.harness_usage.record_usage", lambda **kw: calls.append(kw))
    apply_rate_limit_event({"status": "allowed_warning"}, receipt=default_receipt('claude-code'))
    assert calls[0]["provider"] == "claude-code"
    assert calls[0]["verdict"]["source"] == "rate_limit_event"


def test_model_scoped_rejection_limits_only_its_tier(isolated_db):
    from connections import harness_usage
    from connections.models import ConnectionStatus, ConnectionType, upsert_connection

    upsert_connection(type=ConnectionType.CLI, provider="claude-code",
                      status=ConnectionStatus.AVAILABLE, config={"binary": "/bin/test"})
    apply_rate_limit_event({"status": "rejected", "rateLimitType": "seven_day_opus", "resetsAt": 4102444800}, receipt=default_receipt('claude-code'))

    usage = harness_usage.stored_usage(provider="claude-code", account_id=default_id("claude-code"))
    sonnet = harness_usage.project_for_model(usage, model="claude-code/sonnet")
    opus = harness_usage.project_for_model(usage, model="claude-code/opus")
    assert sonnet["usable"] and sonnet["windows"] == []
    assert (opus["usable"], opus["reason"]) == (False, "seven_day_opus_limit")
    assert [window["kind"] for window in opus["windows"]] == ["seven_day_opus"]
    assert harness_usage.active_limit(provider="claude-code", account_id=default_id("claude-code")) is None


def test_untyped_rejection_is_an_observation_not_a_verdict(isolated_db):
    from connections import harness_usage
    from connections.models import ConnectionStatus, ConnectionType, upsert_connection

    upsert_connection(type=ConnectionType.CLI, provider="claude-code",
                      status=ConnectionStatus.AVAILABLE, config={"binary": "/bin/test"})
    apply_rate_limit_event({"status": "rejected"}, receipt=default_receipt('claude-code'))
    apply_rate_limit_event({"status": "rejected", "rateLimitType": "overage", "resetsAt": 4102444800}, receipt=default_receipt('claude-code'))

    usage = harness_usage.stored_usage(provider="claude-code", account_id=default_id("claude-code")) or {}
    assert not usage.get("vendor_verdicts")
    for model in (None, "claude-code/sonnet", "claude-code/opus", "claude-code/haiku"):
        assert harness_usage.project_for_model(usage, model=model)["usable"]


def test_account_window_rejection_limits_the_account(isolated_db):
    from connections import harness_usage
    from connections.models import ConnectionStatus, ConnectionType, upsert_connection

    upsert_connection(type=ConnectionType.CLI, provider="claude-code",
                      status=ConnectionStatus.AVAILABLE, config={"binary": "/bin/test"})
    apply_rate_limit_event({"status": "rejected", "rateLimitType": "seven_day", "resetsAt": 4102444800}, receipt=default_receipt('claude-code'))

    projection = harness_usage.project_for_model(harness_usage.stored_usage(provider="claude-code", account_id=default_id("claude-code")))
    assert (projection["state"], projection["usable"], projection["reason"]) == ("limited", False, "seven_day_limit")


def test_rate_limit_event_utilization_is_stored_as_percent(isolated_db):
    from connections import harness_usage
    from connections.models import ConnectionStatus, ConnectionType, upsert_connection

    upsert_connection(type=ConnectionType.CLI, provider="claude-code",
                      status=ConnectionStatus.AVAILABLE, config={"binary": "/bin/test"})
    apply_rate_limit_event({"status": "allowed", "rateLimitType": "five_hour", "utilization": 0.1, "resetsAt": 4102444800}, receipt=default_receipt('claude-code'))
    apply_rate_limit_event({"status": "allowed", "rateLimitType": "seven_day", "utilization": 0.55, "resetsAt": 4102444800}, receipt=default_receipt('claude-code'))
    apply_rate_limit_event({"status": "allowed", "rateLimitType": "seven_day_sonnet", "resetsAt": 4102444800}, receipt=default_receipt('claude-code'))

    windows = {w["kind"]: w for w in harness_usage.stored_usage(provider="claude-code", account_id=default_id("claude-code"))["windows"].values()}
    assert windows["seven_day"]["used_percent"] == 55
    assert windows["five_hour"]["used_percent"] == 10
    assert windows["seven_day_sonnet"].get("used_percent") is None


def test_a_live_event_naming_a_non_plan_meter_never_moves_the_account_verdict(isolated_db):
    """Only the plan's own meter may move the account verdict, its facts or its reset."""
    from connections import harness_usage
    from connections.models import ConnectionStatus, ConnectionType, upsert_connection

    upsert_connection(type=ConnectionType.CLI, provider="claude-code",
                      status=ConnectionStatus.AVAILABLE, config={"binary": "/bin/test"})
    receipt = default_receipt("claude-code")
    harness_usage.record_limited(provider="claude-code", reason="five_hour_limit",
                                 resets_at="2100-01-01T00:00:00+00:00", source="terminal_refusal",
                                 receipt=receipt)
    before = harness_usage.stored_usage(provider="claude-code", account_id=default_id("claude-code"))
    assert harness_usage.project_for_model(before)["usable"] is False

    # `nimbus_quill` is a vendor window name that is not a plan window (``window_scope`` is None).
    apply_rate_limit_event({"status": "allowed", "rateLimitType": "nimbus_quill", "utilization": 0.1,
                            "resetsAt": 4102444800}, receipt=receipt)

    after = harness_usage.stored_usage(provider="claude-code", account_id=default_id("claude-code"))
    assert after.get("facts") == before.get("facts")
    assert harness_usage.verdicts(after) == harness_usage.verdicts(before)
    assert harness_usage.project_for_model(after)["usable"] is False


@pytest.mark.parametrize(
    "event",
    [
        # A named meter that maps to no plan window, granting overage: writing the
        # account-wide allowed verdict here released the plan hold.
        {"status": "rejected", "overageStatus": "allowed", "rateLimitType": "overage", "resetsAt": 4102444800},
        # A value that cannot name a meter is not the unnamed account signal either.
        {"status": "allowed", "rateLimitType": 42},
    ],
)
def test_a_non_plan_or_unnameable_meter_stays_an_observation(isolated_db, event):
    """Only the unnamed account signal or a recognized plan window may move the account verdict."""
    from connections import harness_usage
    from connections.models import ConnectionStatus, ConnectionType, upsert_connection

    upsert_connection(type=ConnectionType.CLI, provider="claude-code",
                      status=ConnectionStatus.AVAILABLE, config={"binary": "/bin/test"})
    receipt = default_receipt("claude-code")
    harness_usage.record_limited(provider="claude-code", reason="five_hour_limit",
                                 resets_at="2100-01-01T00:00:00+00:00", source="terminal_refusal",
                                 receipt=receipt)
    before = harness_usage.stored_usage(provider="claude-code", account_id=default_id("claude-code"))
    assert harness_usage.project_for_model(before)["usable"] is False

    apply_rate_limit_event({**event, "utilization": 0.1}, receipt=receipt)

    after = harness_usage.stored_usage(provider="claude-code", account_id=default_id("claude-code"))
    assert harness_usage.verdicts(after) == harness_usage.verdicts(before)
    assert harness_usage.project_for_model(after)["usable"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('release_claims_slowly', [False, True])
async def test_recheck_receipt_loop_fifo_and_terminal_settlement(accounts, notification_owner, control, monkeypatch,
                                                                  release_claims_slowly):
    from connections import harness_usage_check as checks, harness_usage
    from infra import unavailable_model_recovery as recovery
    from infra.thread_driver import finish_turn
    from infra.state.ws import notify_models_changed
    from subagents.codex_usage_observer import observe_account_limits
    from subagents.vendor_write_admission import session_receipt

    db, default, *_ = accounts
    if release_claims_slowly:
        # The cleared limit wakes the head twice, by the connection-limit release and by
        # catalog recovery, and either may claim it. Force the release to claim, with
        # post-claim work that outlasts any fixed count of loop yields.
        monkeypatch.setattr(recovery, '_held_thread_ids', lambda: [])
        resolve = recovery.resolve_or_hold

        async def slow_resolve(model, batch):
            await asyncio.sleep(0.05)
            return await resolve(model, batch)

        monkeypatch.setattr(recovery, 'resolve_or_hold', slow_resolve)
    _known_default(db, default)
    harness_usage.record_limited(reason='limit', resets_at=None, source='test', provider='codex', receipt=default_receipt('codex'))
    _accept(db, 'm')
    _held(db, cause='selected_model_unavailable')
    unbound = control(binary='/bin/test', task=None, control_only=True)
    try:
        await observe_account_limits(unbound)
    finally:
        await unbound.close()
    notify_models_changed()
    await drain()
    assert harness_usage.active_limit(provider='codex', account_id=default_id('codex')) is not None
    assert notification_owner == []
    assert session_receipt(unbound) is None
    refused = _row(db)
    assert (refused['delivery_state'], refused['write_outcome'], refused['active_turn_id']) == ('queued', 'proved_no_write', None)
    assert _kind(db) == ('model', 'model')
    await checks._check('codex', default['id'])
    await drain()
    assert harness_usage.active_limit(provider='codex', account_id=default_id('codex')) is None
    assert len(notification_owner) == 1
    batch = notification_owner[0]
    assert [item.message_id for item in batch.items] == ['m']
    assert batch.vendor_receipt.account_id == default['id']
    after = _row(db)
    assert (after['delivery_state'], after['delivery_error'], after['write_outcome']) == ('delivered', None, 'accepted')
    finish_turn(batch.turn_id, outcome='completed', ended_at='t-terminal',
                successor_turn_id='test-successor', boot_epoch='test', db=db)
    assert db.execute('SELECT status FROM active_turns WHERE id=?', (batch.turn_id,)).fetchone()[0] == 'completed'
    notify_models_changed()
    await drain()
    assert len(notification_owner) == 1
    assert _row(db) == after
    assert all(session.closed for session in control.instances)


@pytest.mark.asyncio
async def test_old_default_recheck_cannot_clear_new_login(accounts, notification_owner, control):
    from connections import harness_usage_check as checks, harness_usage
    from connections.account_receipts import capture_receipt

    db, default, *_ = accounts
    _known_default(db, default)
    old = capture_receipt('codex', account_id=default['id'], db=db)

    def new_login():
        _known_default(db, default, login=2)
        harness_usage.record_limited(reason='new-login-limit', resets_at=None, source='test', provider='codex', receipt=default_receipt('codex'))

    control.mutation = staticmethod(new_login)
    await checks._check('codex', default['id'])
    await drain()
    assert harness_usage.active_limit(provider='codex', account_id=default_id('codex'))['reason'] == 'new-login-limit'
    assert capture_receipt('codex', account_id=default['id'], db=db) != old
    assert notification_owner == []


@pytest.mark.asyncio
async def test_unknown_default_receipt_refuses_and_preserves_custom_usage(accounts, notification_owner, control):
    from connections import harness_usage_check as checks, harness_usage
    from connections.harness_usage import stored_usage
    from vendor_account_fixtures import _ready

    db, default, custom, _ = accounts
    _ready(db, custom)
    harness_usage.record_limited(reason='default-limit', resets_at=None, source='test', provider='codex', receipt=default_receipt('codex'))
    harness_usage.record_limited(reason='custom-limit', resets_at=None, source='test', provider='codex', receipt=receipt_of('codex', custom['id']))
    custom_before = stored_usage(provider='codex', account_id=custom['id'], db=db)
    # The Default is signed out: it has no current login to read for.
    from connections.vendor_accounts import get_account, set_account_state
    set_account_state(default['id'], 'signed_out', expected_revision=get_account(default['id'], db=db)['revision'], db=db)
    db.commit()
    default_before = stored_usage(provider='codex', account_id=default['id'], db=db)
    await checks._check('codex', default['id'])
    await drain()
    assert control.instances == []  # no vendor read starts for an account with no current login
    assert stored_usage(provider='codex', account_id=default['id'], db=db) == default_before
    assert stored_usage(provider='codex', account_id=custom['id'], db=db) == custom_before
    assert notification_owner == []


@pytest.mark.asyncio
async def test_current_custom_refresh_is_independent_of_default_recheck(accounts, notification_owner, control, monkeypatch):
    from connections import harness_models, harness_usage, harness_usage_check as checks
    from connections.account_discovery_types import StagedDiscovery
    from connections.vendor_accounts import get_account
    from connections.harness_usage import stored_usage
    from vendor_account_fixtures import _ready

    db, default, custom, other = accounts
    _known_default(db, default)
    _ready(db, custom)
    _ready(db, other)
    harness_usage.record_limited(reason='limit', resets_at=None, source='test', provider='codex', receipt=default_receipt('codex'))
    custom_before = get_account(custom['id'], db=db)
    other_before = get_account(other['id'], db=db)
    await checks._check('codex', default['id'])
    await drain()
    assert get_account(custom['id'], db=db) == custom_before
    assert get_account(other['id'], db=db) == other_before
    default_before = stored_usage(provider='codex', account_id=default['id'], db=db)
    calls = []

    async def adapter(binary, *, context):
        calls.append(context)
        assert context.account_id == custom['id']
        return StagedDiscovery({'models': ['custom']}, usage_report={'ordinaryUsageAllowed': True})

    monkeypatch.setitem(harness_models._ADAPTERS, 'codex', (
        adapter, lambda payload, **kwargs: [{'selector_id': 'custom', 'available': True}], 'synthetic'))
    result = await harness_models.refresh_provider('codex', reason='manual', account_id=custom['id'],
                                                   binary='/synthetic/codex', harness_version='test', db=db)
    assert result.status == 'ready' and result.account_id == custom['id']
    assert len(calls) == 1
    assert stored_usage(provider='codex', account_id=default['id'], db=db) == default_before
    assert get_account(other['id'], db=db) == other_before
    assert notification_owner == []


def test_allowed_and_sparse_updates_preserve_window_timestamp(isolated_db) -> None:
    # Spending past the plan limit is on, so the vendor's allowed verdict lifts the hold.
    _seed("codex")
    harness_usage.set_allow_overage("codex", default_id("codex"), True)
    db = _db()
    observed = "2026-09-30T00:00:00+00:00"
    harness_usage.record_usage(provider="codex", windows=[{
        "vendor": "codex", "kind": "codex_primary", "used_percent": 100,
        "resets_at": "2026-10-01T00:00:00+00:00", "observed_at": observed,
        "source": "rate_limit_update",
    }], verdict={"state": "limited", "source": "rate_limit_update", "observed_at": observed}, db=db, receipt=default_receipt("codex", db=db))

    harness_usage.record_allowed(provider="codex", source="turn_completed", db=db, receipt=default_receipt("codex", db=db))
    harness_usage.record_usage(provider="codex", windows=[{
        "vendor": "codex", "kind": "codex_secondary", "used_percent": 20,
        "observed_at": "2026-09-30T01:00:00+00:00", "source": "rate_limit_update",
    }], db=db, receipt=default_receipt("codex", db=db))

    usage = harness_usage.stored_usage(provider="codex", db=db, account_id=default_id("codex", db=db))
    primary = next(window for window in usage["windows"].values() if window["kind"] == "codex_primary")
    assert primary["observed_at"] == observed
    assert usage["vendor_verdicts"]["null"]["state"] == "allowed"


def test_newer_sparse_window_preserves_percentage_and_its_observation_time(isolated_db) -> None:
    _seed("claude-code")
    db = _db()
    first = "2026-09-30T00:00:00+00:00"
    later = "2026-09-30T01:00:00+00:00"
    harness_usage.record_usage(provider="claude-code", windows=[{
        "vendor": "claude-code", "kind": "five_hour", "used_percent": 62,
        "vendor_status": "allowed", "resets_at": "2026-09-30T05:00:00+00:00",
        "observed_at": first, "source": "discovery",
    }], db=db, receipt=default_receipt("claude-code", db=db))

    harness_usage.record_usage(provider="claude-code", windows=[{
        "vendor": "claude-code", "kind": "five_hour", "vendor_status": "rejected",
        "resets_at": "2026-09-30T06:00:00+00:00", "observed_at": later,
        "source": "rate_limit_event",
    }], db=db, receipt=default_receipt("claude-code", db=db))

    window = next(iter(harness_usage.stored_usage(provider="claude-code", db=db, account_id=default_id("claude-code", db=db))["windows"].values()))
    assert window["used_percent"] == 62
    assert window["used_percent_observed_at"] == first
    assert window["used_percent_resets_at"] == "2026-09-30T05:00:00+00:00"
    assert (window["vendor_status"], window["resets_at"], window["observed_at"]) == (
        "rejected", "2026-09-30T06:00:00+00:00", later,
    )


def test_older_window_frame_never_overwrites_newer_fields(isolated_db) -> None:
    _seed("claude-code")
    db = _db()
    newer = "2026-09-30T01:00:00+00:00"
    harness_usage.record_usage(provider="claude-code", windows=[{
        "vendor": "claude-code", "kind": "five_hour", "used_percent": 62,
        "vendor_status": "rejected", "observed_at": newer, "source": "rate_limit_event",
    }], db=db, receipt=default_receipt("claude-code", db=db))

    harness_usage.record_usage(provider="claude-code", windows=[{
        "vendor": "claude-code", "kind": "five_hour", "used_percent": 10,
        "vendor_status": "allowed", "observed_at": "2026-09-30T00:00:00+00:00", "source": "discovery",
    }], db=db, receipt=default_receipt("claude-code", db=db))

    window = next(iter(harness_usage.stored_usage(provider="claude-code", db=db, account_id=default_id("claude-code", db=db))["windows"].values()))
    assert (window["used_percent"], window["vendor_status"], window["observed_at"]) == (62, "rejected", newer)


def test_window_staleness_uses_percentage_observation_time() -> None:
    window = {
        "kind": "five_hour", "used_percent": 62,
        "used_percent_observed_at": "2026-09-30T00:00:00+00:00",
        "observed_at": "2026-09-30T01:00:00+00:00",
        "window_minutes": 30,
    }

    projection = harness_usage.project_for_model(
        {"windows": {"claude-code:five_hour:account": window}},
        now=datetime(2026, 9, 30, 0, 45, tzinfo=timezone.utc),
    )

    assert projection["windows"][0]["state"] == "unknown"


def test_sparse_frame_after_reset_drops_the_previous_windows_percentage(isolated_db) -> None:
    _seed("codex")
    db = _db()
    harness_usage.record_usage(provider="codex", windows=[{
        "vendor": "codex", "kind": "codex_primary", "used_percent": 95, "window_minutes": 10080,
        "resets_at": "2026-09-30T12:00:00+00:00", "observed_at": "2026-09-30T11:00:00+00:00",
        "source": "rate_limit_update",
    }], db=db, receipt=default_receipt("codex", db=db))

    harness_usage.record_usage(provider="codex", windows=[{
        "vendor": "codex", "kind": "codex_primary", "window_minutes": 10080,
        "resets_at": "2026-10-07T12:00:00+00:00", "observed_at": "2026-09-30T13:00:00+00:00",
        "source": "rate_limit_update",
    }], db=db, receipt=default_receipt("codex", db=db))

    window = next(iter(harness_usage.stored_usage(provider="codex", db=db, account_id=default_id("codex", db=db))["windows"].values()))
    assert "used_percent" not in window
    assert window["resets_at"] == "2026-10-07T12:00:00+00:00"


def test_percentage_is_stale_once_its_own_window_resets() -> None:
    window = {
        "kind": "codex_primary", "used_percent": 95,
        "used_percent_observed_at": "2026-09-30T11:00:00+00:00",
        "used_percent_resets_at": "2026-09-30T12:00:00+00:00",
        "resets_at": "2026-10-07T12:00:00+00:00",
        "window_minutes": 10080,
    }

    projection = harness_usage.project_for_model(
        {"windows": {"codex:codex_primary:account": window}},
        now=datetime(2026, 9, 30, 13, 0, tzinfo=timezone.utc),
    )

    assert projection["windows"][0]["state"] == "unknown"


def test_live_event_wins_an_equal_time_verdict(isolated_db) -> None:
    _seed("claude-code")
    db = _db()
    observed = "2026-09-30T00:00:00+00:00"
    harness_usage.record_usage(provider="claude-code", verdict={
        "state": "limited", "source": "discovery", "observed_at": observed,
    }, db=db, receipt=default_receipt("claude-code", db=db))
    harness_usage.record_usage(provider="claude-code", verdict={
        "state": "allowed", "source": "rate_limit_event", "observed_at": observed,
    }, db=db, receipt=default_receipt("claude-code", db=db))
    assert harness_usage.stored_usage(provider="claude-code", db=db, account_id=default_id("claude-code", db=db))["vendor_verdicts"]["null"]["state"] == "allowed"


def test_unknown_is_usable_but_active_limited_is_not(isolated_db) -> None:
    _seed("codex")
    db = _db()
    harness_usage.record_check_failed(provider="codex", error="unavailable", db=db, receipt=default_receipt("codex", db=db))
    assert harness_usage.project_for_model(harness_usage.stored_usage(provider="codex", db=db, account_id=default_id("codex", db=db)))["usable"]

    reset = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    harness_usage.record_limited(provider="codex", reason="limit", resets_at=reset, source="test", db=db, receipt=default_receipt("codex", db=db))
    projection = harness_usage.project_for_model(harness_usage.stored_usage(provider="codex", db=db, account_id=default_id("codex", db=db)))
    assert (projection["state"], projection["usable"], projection["resets_at"]) == ("limited", False, reset)


def test_catalog_reads_each_harness_default_by_its_account_id(isolated_db) -> None:
    from llm.account_catalog import project_accounts

    _seed("claude-code")
    _seed("codex")
    rows = project_accounts([{"id": f"{harness}/test", "litellm_model": f"{harness}/test", "connection": harness,
                              "status": "ready", "capabilities": {}} for harness in ("claude-code", "codex")])

    assert {row["connection"]: next(item["account_id"] for item in row["runs_on"] if item["selected"])
            for row in rows} == {
        "claude-code": default_id("claude-code"), "codex": default_id("codex")}


def test_codex_full_window_with_ordinary_usage_allowed_stays_usable_when_overage_is_on(isolated_db) -> None:
    _seed("codex")
    harness_usage.set_allow_overage("codex", default_id("codex"), True)
    projection = _read_full_window_on_credits(_db())

    assert (projection["state"], projection["usable"]) == ("allowed", True)
    assert projection["allow_overage"] is True
    assert [window["used_percent"] for window in projection["windows"]] == [100]


def test_codex_full_window_on_credits_is_held_by_default(isolated_db) -> None:
    _seed("codex")
    projection = _read_full_window_on_credits(_db())

    assert (projection["state"], projection["usable"]) == ("limited", False)
    assert projection["reason"] == "plan_limit_overage_off"
    assert projection["allow_overage"] is False


def test_conflicting_codex_verdict_is_a_failed_check_not_a_recovery(isolated_db) -> None:
    _seed("codex")
    db = _db()
    reset = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    harness_usage.record_limited(provider="codex", reason="limit", resets_at=reset, source="test", db=db, receipt=default_receipt("codex", db=db))
    harness_usage.record_rate_limits_read({
        "ordinaryUsageAllowed": True, "rateLimits": {"rateLimitReachedType": "primary"},
    }, provider="codex", db=db, receipt=default_receipt("codex", db=db))

    projection = harness_usage.project_for_model(harness_usage.stored_usage(provider="codex", db=db, account_id=default_id("codex", db=db)))
    assert (projection["state"], projection["usable"]) == ("limited", False)
    assert projection["check"]["error"] == "Codex returned conflicting usage verdicts"


def test_expired_limit_projects_as_usable_unknown(isolated_db) -> None:
    _seed("codex")
    db = _db()
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    harness_usage.record_limited(provider="codex", reason="limit", resets_at=past, source="test", db=db, receipt=default_receipt("codex", db=db))

    projection = harness_usage.project_for_model(harness_usage.stored_usage(provider="codex", db=db, account_id=default_id("codex", db=db)))
    assert (projection["state"], projection["usable"], projection["reason"]) == ("unknown", True, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude-code", "codex"])
async def test_recheck_restores_a_limited_account_and_notifies(monkeypatch, provider, _checks_clean):
    _seed(provider)
    if provider == "codex":
        from vendor_account_fixtures import record_default_identity
        record_default_identity("codex", {"account_fp": "synthetic-usage-check"})
    _limited(provider)
    notified = []
    monkeypatch.setattr(checks, "_binary_for", lambda _p: asyncio.sleep(0, result="/bin/test"))
    monkeypatch.setattr("infra.state.ws.notify_models_changed", lambda: notified.append(True))
    if provider == "claude-code":
        async def spawn(*_args, **_kwargs):
            return _Process(_usage_response({"rate_limits_available": True, "rate_limits": {"five_hour": {"utilization": 1}}}))
        monkeypatch.setattr(checks.asyncio, "create_subprocess_exec", spawn)
    else:
        class Session:
            def __init__(self, **_kwargs): pass
            async def start(self): pass
            async def close(self, **_kwargs): pass
            async def _request(self, _method, _params):
                return {"ordinaryUsageAllowed": True, "rateLimits": {}}
        monkeypatch.setattr("subagents.codex_app_server.CodexAppServerSession", Session)
    await checks._check(provider, default_id(provider))
    assert codex_usage.active_limit(provider=provider, account_id=default_id(provider)) is None
    assert notified == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude-code", "codex"])
async def test_recheck_keeps_a_limit_and_updates_reset(monkeypatch, provider, _checks_clean):
    _seed(provider)
    if provider == "codex":
        from vendor_account_fixtures import record_default_identity
        record_default_identity("codex", {"account_fp": "synthetic-usage-check"})
    _limited(provider)
    reset = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
    monkeypatch.setattr(checks, "_binary_for", lambda _p: asyncio.sleep(0, result="/bin/test"))
    monkeypatch.setattr("infra.state.ws.notify_models_changed", lambda: None)
    if provider == "claude-code":
        async def spawn(*_args, **_kwargs):
            return _Process(_usage_response({"rate_limits_available": True, "rate_limits": {"five_hour": {"utilization": 100, "resets_at": reset}}}))
        monkeypatch.setattr(checks.asyncio, "create_subprocess_exec", spawn)
    else:
        class Session:
            def __init__(self, **_kwargs): pass
            async def start(self): pass
            async def close(self, **_kwargs): pass
            async def _request(self, _method, _params):
                return {"ordinaryUsageAllowed": False, "rateLimits": {"rateLimitReachedType": "limit", "primary": {"usedPercent": 100, "resetsAt": reset}}}
        monkeypatch.setattr("subagents.codex_app_server.CodexAppServerSession", Session)
    await checks._check(provider, default_id(provider))
    assert codex_usage.active_limit(provider=provider, account_id=default_id(provider))["resets_at"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["timeout", "failed", "missing"])
async def test_indeterminate_claude_recheck_keeps_limit_and_marks_failed(monkeypatch, error, _checks_clean):
    _seed("claude-code")
    _limited("claude-code")
    monkeypatch.setattr(checks, "_binary_for", lambda _p: asyncio.sleep(0, result="/bin/test"))
    monkeypatch.setattr("infra.state.ws.notify_models_changed", lambda: None)
    response = None if error == "missing" else _usage_response({"rate_limits_available": True, "rate_limits": {}})
    process = _Process(response, error="timeout" if error == "timeout" else ("bad" if error == "failed" else ""))
    if error == "failed": process.returncode = 1
    async def spawn(*_args, **_kwargs): return process
    monkeypatch.setattr(checks.asyncio, "create_subprocess_exec", spawn)
    await checks._check("claude-code", default_id("claude-code"))
    state = codex_usage.active_limit(provider="claude-code", account_id=default_id("claude-code"))
    assert state and state["check_error"]


@pytest.mark.asyncio
async def test_scheduler_throttles_and_is_single_flight(monkeypatch, _checks_clean):
    started, release = [], asyncio.Event()
    async def check(provider, account_id):
        started.append((provider, account_id))
        await release.wait()
    monkeypatch.setattr(checks, "_check", check)
    checks._schedule("codex", "account-1")
    checks._schedule("codex", "account-1")
    await asyncio.sleep(0)
    assert started == [("codex", "account-1")] and checks.is_checking("account-1")
    release.set()
    await asyncio.gather(*checks._CHECKS.values())
    checks._schedule("codex", "account-1")
    assert started == [("codex", "account-1")]


@pytest.mark.asyncio
async def test_a_new_login_during_the_prior_logins_read_is_read_when_it_ends(monkeypatch, _checks_clean):
    import infra.app_loop as app_loop

    logins, release, prior_running = iter(["prior-login", "new-login"]), asyncio.Event(), asyncio.Event()
    read_for = []
    monkeypatch.setattr(checks, "_capture", lambda provider, account_id: SimpleNamespace(receipt=next(logins), is_default=True))
    monkeypatch.setattr(checks, "_binary_for", lambda _provider: asyncio.sleep(0, result="/synthetic/claude"))
    async def read(_binary, receipt, *, context=None):
        read_for.append(receipt)
        if receipt == "prior-login":
            prior_running.set()
            await release.wait()
    monkeypatch.setattr(checks, "_check_claude", read)
    monkeypatch.setattr("infra.state.ws.notify_models_changed", lambda: None)
    submitted = []
    monkeypatch.setattr(app_loop, "submit", lambda coro: submitted.append(asyncio.ensure_future(coro)))

    checks._schedule("claude-code", "account-1")
    await asyncio.wait_for(prior_running.wait(), 5)
    checks.schedule_account_check("claude-code", "account-1", new_login=True)
    await asyncio.gather(*submitted)
    assert read_for == ["prior-login"]

    release.set()
    while checks._CHECKS:
        await asyncio.gather(*list(checks._CHECKS.values()))
    assert read_for == ["prior-login", "new-login"]


@pytest.mark.asyncio
async def test_a_new_login_is_read_inside_the_prior_logins_throttle(monkeypatch, _checks_clean):
    import infra.app_loop as app_loop

    started = []
    async def check(provider, account_id):
        started.append((provider, account_id))
    monkeypatch.setattr(checks, "_check", check)
    submitted = []
    monkeypatch.setattr(app_loop, "submit", lambda coro: submitted.append(asyncio.ensure_future(coro)))
    checks._schedule("claude-code", "account-1")
    await asyncio.gather(*checks._CHECKS.values())

    checks.schedule_account_check("claude-code", "account-1")
    await asyncio.gather(*submitted)
    assert started == [("claude-code", "account-1")]

    checks.schedule_account_check("claude-code", "account-1", new_login=True)
    await asyncio.gather(*submitted)
    await asyncio.gather(*checks._CHECKS.values())
    assert started == [("claude-code", "account-1")] * 2


def test_account_usage_reports_checking(monkeypatch, _checks_clean):
    from api.vendor_accounts import _usage
    from connections.vendor_accounts import default_account

    _seed("codex")
    account = default_account("codex")
    assert _usage(account)["checking"] is False
    monkeypatch.setattr("connections.harness_usage_check.is_checking", lambda _p: True)
    assert _usage(account)["checking"] is True


def test_full_read_then_live_update_is_one_weekly_window_with_the_latest_value(isolated_db) -> None:
    # The production shape: the main snapshot is also the `codex` bucket.
    _seed_meter_connection("codex")
    db = _db()
    _read(_snapshot("codex", 6), {"codex": _snapshot("codex", 6)}, db)

    _live_update(_snapshot("codex", 7))

    weekly = [window for window in _stored(db).values() if window["window_minutes"] == WEEK]
    assert [(window["used_percent"], window["source"]) for window in weekly] == [(7, "rate_limit_update")]
    assert _shown(db) == [("codex_primary", 7)]


def test_update_without_a_limit_id_is_the_default_meter(isolated_db) -> None:
    _seed_meter_connection("codex")
    db = _db()
    _read(_snapshot(None, 6), {"codex": _snapshot(None, 6)}, db)

    _live_update({"primary": {"usedPercent": 9, "windowDurationMins": WEEK, "resetsAt": _reset()}})

    assert list(_stored(db)) == ["codex:codex_primary:account"]
    assert _shown(db) == [("codex_primary", 9)]


def test_another_meter_keeps_both_its_windows_and_is_never_a_plan_window(isolated_db) -> None:
    _seed_meter_connection("codex")
    db = _db()
    other = _snapshot("codex_bengalfox", 40, secondary=(55, 300))
    _read(_snapshot("codex", 6), {"codex": _snapshot("codex", 6), "codex_bengalfox": other}, db)

    assert sorted(_stored(db)) == [
        "codex:codex_bengalfox:codex_primary:account", "codex:codex_bengalfox:codex_secondary:account",
        "codex:codex_primary:account",
    ]
    assert _shown(db) == [("codex_primary", 6)]


def test_live_update_for_another_meter_never_overwrites_the_plan_window(isolated_db) -> None:
    _seed_meter_connection("codex")
    db = _db()
    _read(_snapshot("codex", 6), {"codex": _snapshot("codex", 6)}, db)

    _live_update(_snapshot("codex_bengalfox", 88))

    assert _stored(db)["codex:codex_primary:account"]["used_percent"] == 6
    assert _stored(db)["codex:codex_bengalfox:codex_primary:account"]["used_percent"] == 88
    assert _shown(db) == [("codex_primary", 6)]


def test_the_full_read_names_the_plan_meter_when_it_is_not_the_default(isolated_db) -> None:
    _seed_meter_connection("codex")
    db = _db()
    # Before any read names another meter, an update for it is a by-limit bucket.
    _live_update(_snapshot("codex_team", 30))
    assert list(_stored(db)) == ["codex:codex_team:codex_primary:account"]

    _read(_snapshot("codex_team", 31), {"codex_team": _snapshot("codex_team", 31)}, db)
    assert list(_stored(db)) == ["codex:codex_primary:account"]

    _live_update(_snapshot("codex_team", 32))
    assert list(_stored(db)) == ["codex:codex_primary:account"]
    assert _shown(db) == [("codex_primary", 32)]


def test_stored_duplicate_from_version_1_is_gone_on_the_first_read(isolated_db) -> None:
    _seed_meter_connection("codex", usage=_version_1_blob())
    db = _db()

    assert _shown(db) == [("codex_primary", 7)]
    assert list(harness_usage.stored_usage(provider="codex", account_id=default_id("codex", db=db), db=db)["windows"]) == ["codex:codex_primary:account"]


def test_first_write_after_upgrade_persists_the_current_version_without_the_duplicate(isolated_db) -> None:
    _seed_meter_connection("codex", usage=_version_1_blob())
    db = _db()

    harness_usage.record_allowed(provider="codex", source="turn_completed", db=db, receipt=default_receipt("codex", db=db))

    from connections.vendor_accounts import resolve_account_id

    identity = resolve_account_id("codex", db=db)
    row = db.execute("SELECT usage_json FROM vendor_accounts WHERE id=?", (identity,)).fetchone()
    usage = json.loads(row["usage_json"])
    config = db.execute("SELECT config FROM connections WHERE provider='codex'").fetchone()[0]
    assert "usage" not in json.loads(config or "{}")
    assert (usage["version"], list(usage["windows"])) == (usage_store.USAGE_VERSION, ["codex:codex_primary:account"])


def test_projection_shows_plan_windows_only_each_under_the_scope_its_kind_names() -> None:
    def shown(model: str) -> list[tuple[str, str | None]]:
        projection = harness_usage.project_for_model(_claude_windows(), model=model)
        return [(window["kind"], window["scope_model"]) for window in projection["windows"]]

    assert shown("claude-code/opus") == [("five_hour", None), ("seven_day", None)]
    assert shown("claude-code/sonnet") == [("five_hour", None), ("seven_day", None), ("seven_day_sonnet", "sonnet")]


def test_a_v1_fraction_never_survives_a_newer_event_without_utilization(isolated_db, no_stops) -> None:
    db = get_db()
    _seed_stored_usage(db, _v1({"claude-code:seven_day_sonnet:sonnet": _V1_SONNET_EVENT}))

    claude_usage.apply_rate_limit_event(
        {"status": "allowed", "rateLimitType": "seven_day_sonnet", "resetsAt": _RESET}, db=db, receipt=default_receipt('claude-code', db=db),
    )

    stored = _raw_usage(db)
    [sonnet] = _windows(stored, "seven_day_sonnet")
    assert "used_percent" not in sonnet  # no reading, never 99% left
    assert stored["version"] == usage_store.USAGE_VERSION
    projected = harness_usage.project_for_model(stored, model="claude-code/sonnet")
    assert all(window.get("used_percent") != 0.55 for window in projected.get("windows", []))


def test_a_v1_blob_reads_without_event_percentages_before_any_write(isolated_db) -> None:
    db = get_db()
    _seed_stored_usage(db, _v1({
        "claude-code:seven_day_sonnet:sonnet": _V1_SONNET_EVENT,
        "claude-code:five_hour:account": _V1_FIVE_HOUR_DISCOVERY,
    }))

    for usage in (harness_usage.stored_usage(provider=_PROVIDER, account_id=default_id(_PROVIDER, db=db), db=db), harness_usage.stored_usage(provider=_PROVIDER, db=db, account_id=default_id(_PROVIDER, db=db))):
        assert usage is not None
        [sonnet] = _windows(usage, "seven_day_sonnet")
        assert "used_percent" not in sonnet and sonnet["resets_at"] == _RESET
        assert usage["version"] == usage_store.USAGE_VERSION
        [five_hour] = _windows(usage, "five_hour")
        assert five_hour["used_percent"] == 40.0  # a discovery percentage was always 0-100


def test_a_current_blob_keeps_every_event_percentage(isolated_db) -> None:
    db = get_db()
    current = {**_V1_SONNET_EVENT, "used_percent": 1.0}
    _seed_stored_usage(db, {**_v1({"claude-code:seven_day_sonnet:sonnet": current}), "version": usage_store.USAGE_VERSION})
    [sonnet] = _windows(harness_usage.stored_usage(provider=_PROVIDER, account_id=default_id(_PROVIDER, db=db), db=db), "seven_day_sonnet")
    assert sonnet["used_percent"] == 1.0  # a real 1% reading under the current version


@pytest.mark.parametrize(("version", "by_limit_kept"), [(1, False), (2, True)])
def test_each_older_version_takes_only_its_own_steps(version: int, by_limit_kept: bool) -> None:
    usage = usage_store.current({**_v1({
        "claude-code:seven_day_sonnet:sonnet": _V1_SONNET_EVENT, "codex:other:codex_primary:account": _OTHER_METER,
    }), "version": version})

    [sonnet] = _windows(usage, "seven_day_sonnet")
    assert "used_percent" not in sonnet  # a version-2 blob can still hold a version-1 fraction
    assert bool(_windows(usage, "codex_primary")) is by_limit_kept  # version 2 already keys a bucket by its meter
    assert usage["version"] == usage_store.USAGE_VERSION


@pytest.mark.parametrize("raw_scope", [None, "Sonnet", "claude-sonnet-4-5"])
def test_discovery_and_events_write_one_window_per_meter(isolated_db, no_stops, raw_scope) -> None:
    db = get_db()
    report = {"rate_limits": {"seven_day_sonnet": {"utilization": 45, "resets_at": _RESET, "scope_model": raw_scope}}}
    claude_usage.apply_usage_response(report, db=db, receipt=default_receipt('claude-code', db=db))
    claude_usage.apply_rate_limit_event(
        {"status": "allowed", "rateLimitType": "seven_day_sonnet", "utilization": 0.5, "resetsAt": _RESET}, db=db, receipt=default_receipt('claude-code', db=db),
    )

    [sonnet] = _windows(_raw_usage(db), "seven_day_sonnet")
    assert sonnet["used_percent"] == 50.0 and sonnet["scope_model"] == "sonnet"


def test_a_v1_window_under_a_raw_vendor_scope_is_dropped(isolated_db) -> None:
    db = get_db()
    raw_keyed = {**_V1_FIVE_HOUR_DISCOVERY, "kind": "seven_day_sonnet", "scope_model": None, "used_percent": 70.0}
    _seed_stored_usage(db, _v1({"claude-code:seven_day_sonnet:account": raw_keyed}))
    assert _windows(harness_usage.stored_usage(provider=_PROVIDER, account_id=default_id(_PROVIDER, db=db), db=db), "seven_day_sonnet") == []


def test_newer_allowed_tier_never_clears_an_account_limit() -> None:
    account = {"state": "limited", "resets_at": FUTURE, "observed_at": "2026-09-30T01:00:00+00:00"}
    tier = {"state": "allowed", "observed_at": "2026-09-30T02:00:00+00:00"}

    effective = effective_verdict(_usage(null=account, opus=tier), "claude-code/opus", NOW)

    assert effective.blocking == account
    assert effective.latest == tier


def test_tier_limit_blocks_only_models_it_names() -> None:
    tier = {"state": "limited", "resets_at": FUTURE, "observed_at": PAST}
    usage = _usage(opus=tier)

    assert effective_verdict(usage, "claude-code/opus", NOW).blocking == tier
    assert effective_verdict(usage, "claude-code/sonnet", NOW).blocking is None
    assert effective_verdict(usage, None, NOW).blocking is None


def test_newest_active_limit_decides_and_an_expired_one_does_not() -> None:
    expired = {"state": "limited", "resets_at": PAST, "observed_at": "2026-09-30T03:00:00+00:00"}
    older = {"state": "limited", "resets_at": None, "observed_at": "2026-09-29T00:00:00+00:00"}
    newer = {"state": "limited", "resets_at": FUTURE, "observed_at": "2026-09-30T01:00:00+00:00"}

    effective = effective_verdict(_usage(null=older, opus=newer, sonnet=expired), "opus", NOW)
    assert effective.blocking == newer
    assert effective_verdict(_usage(sonnet=expired), "sonnet", NOW).blocking is None


def test_missing_or_malformed_usage_has_no_verdict() -> None:
    cases: tuple[dict[str, Any] | None, ...] = (
        None, {}, {"vendor_verdicts": []}, {"vendor_verdicts": {"null": "limited"}},
    )
    for usage in cases:
        effective = effective_verdict(usage, "claude-code/opus", NOW)
        assert (effective.blocking, effective.latest) == (None, None)


def test_scope_matching_and_window_scope() -> None:
    assert scope_key(None) == scope_key("NULL") == ACCOUNT_SCOPE
    assert scope_key("Opus") == "opus"
    assert scope_matches(None, ACCOUNT_SCOPE)
    assert scope_matches("claude-code/opus", "opus")
    assert not scope_matches("claude-code/sonnet", "opus")
    assert not scope_matches(None, "opus")
    assert window_scope("five_hour") == window_scope("seven_day") == ACCOUNT_SCOPE
    assert window_scope("seven_day_opus") == "opus"
    assert window_scope("seven_day_") is None
    assert window_scope("overage") is None
    assert window_scope(None) is None
    assert window_scope("codex_primary") == window_scope("codex_secondary") == ACCOUNT_SCOPE
    assert window_scope("codex_primary", limit_id="codex_other") is None


@pytest.mark.parametrize('default', [False, True])
def test_old_same_row_same_reset_receipts_cannot_mutate_usage_or_emit(accounts, monkeypatch, default):
    from connections.account_receipts import capture_receipt
    from connections.vendor_accounts import set_account_state
    from connections import harness_usage as usage

    db, legacy, custom, _ = accounts
    account = legacy if default else custom
    if default:
        _known_default(db, account)
    else:
        _ready(db, account)
    old = capture_receipt('codex', account_id=account['id'], db=db)
    reset = '2100-01-01T00:00:00+00:00'
    usage.record_limited(provider='codex', reason='plan_limit', resets_at=reset, source='old',
                         receipt=old, db=db)
    if default:
        _known_default(db, account, login=2)
    else:
        set_account_state(account['id'], 'signed_in', expected_revision=account['revision'], db=db)
    new = capture_receipt('codex', account_id=account['id'], db=db)
    assert new != old
    usage.record_limited(provider='codex', reason='new_plan_limit', resets_at=reset, source='new',
                         receipt=new, db=db)
    before = usage.stored_usage(provider='codex', account_id=account['id'], db=db)
    seen = []
    monkeypatch.setattr('infra.state.ws.schedule_models_changed', lambda: seen.append('changed'))
    usage.record_allowed(provider='codex', source='late', receipt=old, db=db)
    usage.record_usage(provider='codex', facts={'planType': 'foreign'}, receipt=old, db=db)
    usage.record_check_failed(provider='codex', error='late failure', receipt=old, db=db)
    usage.record_rate_limits_read({'ordinaryUsageAllowed': True, 'rateLimits': {'limitId': 'foreign'}},
                                  provider='codex', receipt=old, db=db)
    assert usage.stored_usage(provider='codex', account_id=account['id'], db=db) == before
    assert seen == []


def test_missing_receipt_is_zero_effect_and_a_receipt_writes_only_its_own_account(accounts):
    """A write names its account only through its receipt: no receipt has no current-login fallback,
    and another account's receipt cannot land on this one."""
    from connections import harness_usage as usage
    from connections.account_receipts import capture_receipt

    db, default, custom, other = accounts
    _ready(db, custom)
    _ready(db, other)
    usage.record_usage(provider='codex', receipt=None, facts={'planType': 'wrong'}, db=db)
    for account in (default, custom, other):
        assert usage.stored_usage(provider='codex', account_id=account['id'], db=db) is None
    receipt = capture_receipt('codex', account_id=other['id'], db=db)
    usage.record_usage(provider='codex', receipt=receipt, facts={'planType': 'other'}, db=db)
    assert usage.stored_usage(provider='codex', account_id=custom['id'], db=db) is None
    assert usage.stored_usage(provider='codex', account_id=default['id'], db=db) is None
    assert usage.stored_usage(provider='codex', account_id=other['id'], db=db)['facts'] == {'planType': 'other'}
