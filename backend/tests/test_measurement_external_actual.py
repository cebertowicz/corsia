"""Actual cost of External endpoint usage: subscription, pay per use, or unknown, by the mode on the call's day."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from api.measurement_actual import build_actual
from core.measurement_scope import resolve_grain
from connections import subscription_billing as sb
from infra.database import get_db
from model_identity_fixtures import ALIBABA_HOST, seed_external_server

# The latest window of days is Sep 22 to Sep 28; Sep 29 is the day in progress.
GRAIN = resolve_grain("days", "UTC", now=datetime(2026, 9, 29, 12, tzinfo=timezone.utc))
PARAMS = {"scope_project": "all", "scope_agent": None, "user_id": "steve", "grain": GRAIN}
QWEN = "external@ns/qwen3.8-flash"
FIELDS = ("subscription_records", "unknown_charge_records", "direct_api_dollars", "complete", "external_unset_records")


@pytest.fixture
def today(monkeypatch):
    state = {"day": date(2026, 9, 1)}
    monkeypatch.setattr(sb, "_today", lambda: state["day"])
    return state


def _db():
    db = get_db()
    db.execute("CREATE TEMP TABLE measurement_scope_threads (id TEXT PRIMARY KEY, root_id TEXT)")
    db.execute("INSERT INTO measurement_scope_threads VALUES ('thread', 'thread')")
    seed_external_server(db, "ns", ALIBABA_HOST)
    db.execute("UPDATE network_servers SET created_at='2026-09-01T00:00:00+00:00', updated_at='2026-09-01T00:00:00+00:00'")
    return db


def _call(db, row_id: str, *, model: str = QWEN, day: str = "2026-09-22", prompt: int = 1_000_000,
          completion: int = 0, cost=None) -> None:
    db.execute(
        "INSERT INTO llm_usage (id,thread_id,model,prompt_tokens,completion_tokens,cache_creation_tokens,"
        "cache_read_tokens,total_tokens,cost_usd,source,created_at) VALUES (?,?,?,?,?,0,0,?,?,'conversation',?)",
        (row_id, "thread", model, prompt, completion, prompt + completion, cost, f"{day}T12:00:00Z"),
    )


def _mode(db, server: str, mode, today, *, on: date, cents=None, day=None) -> None:
    today["day"] = on
    revision = sb.endpoint_billing(db, [server])[server]["billing_revision"]
    sb.set_endpoint_billing(server, mode=mode, monthly_price_cents=cents, charge_day=day,
                            expected_billing_revision=revision, db=db)


def _actual(db) -> dict:
    result = build_actual(db, PARAMS)
    return {key: result[key] for key in FIELDS}


def test_an_unset_endpoint_reads_as_unknown_never_zero(isolated_db, today):
    db = _db()
    try:
        _call(db, "a", cost=0.0)  # a legacy placeholder zero is not a charge
        assert _actual(db) == {"subscription_records": 0, "unknown_charge_records": 1,
                               "direct_api_dollars": 0.0, "complete": False, "external_unset_records": 1}
    finally:
        db.close()


def test_pay_per_use_prices_at_the_list_rate_when_the_host_lists_the_model(isolated_db, today):
    db = _db()
    try:
        _mode(db, "ns", "pay_per_use", today, on=date(2026, 9, 5))
        _call(db, "a", prompt=1_000_000, completion=1_000_000)
        assert _actual(db) == {"subscription_records": 0, "unknown_charge_records": 0,
                               "direct_api_dollars": pytest.approx(0.15 + 0.47), "complete": True,
                               "external_unset_records": 0}
    finally:
        db.close()


def test_pay_per_use_of_an_unlisted_model_is_an_unknown_charge_not_zero(isolated_db, today):
    db = _db()
    try:
        _mode(db, "ns", "pay_per_use", today, on=date(2026, 9, 5))
        _call(db, "a", model="external@ns/unlisted-model")
        assert _actual(db) == {"subscription_records": 0, "unknown_charge_records": 1,
                               "direct_api_dollars": 0.0, "complete": False, "external_unset_records": 0}
    finally:
        db.close()


def test_pay_per_use_on_a_host_with_no_price_list_is_unknown(isolated_db, today):
    db = _db()
    try:
        seed_external_server(db, "self", "192.168.1.5", "lan")
        db.execute("UPDATE network_servers SET created_at='2026-09-01T00:00:00+00:00' WHERE id='self'")
        _mode(db, "self", "pay_per_use", today, on=date(2026, 9, 5))
        _call(db, "a", model="external@self/qwen3.8-flash")
        assert _actual(db)["unknown_charge_records"] == 1
    finally:
        db.close()


def test_a_subscription_endpoint_counts_as_subscription_usage(isolated_db, today):
    db = _db()
    try:
        _mode(db, "ns", "subscription", today, on=date(2026, 9, 5), cents=4000, day=21)
        _call(db, "a")
        assert _actual(db) == {"subscription_records": 1, "unknown_charge_records": 0,
                               "direct_api_dollars": 0.0, "complete": True, "external_unset_records": 0}
    finally:
        db.close()


def test_a_mode_switch_does_not_reclassify_earlier_days(isolated_db, today):
    db = _db()
    try:
        _mode(db, "ns", "pay_per_use", today, on=date(2026, 9, 1))
        _mode(db, "ns", "subscription", today, on=date(2026, 9, 23), cents=4000, day=21)
        _call(db, "before", day="2026-09-22", prompt=1_000_000)
        _call(db, "after", day="2026-09-23")
        assert _actual(db) == {"subscription_records": 1, "unknown_charge_records": 0,
                               "direct_api_dollars": pytest.approx(0.15), "complete": True,
                               "external_unset_records": 0}
    finally:
        db.close()


def test_days_before_the_first_mode_and_after_it_is_cleared_are_unknown(isolated_db, today):
    db = _db()
    try:
        _mode(db, "ns", "subscription", today, on=date(2026, 9, 1), cents=4000, day=21)
        _mode(db, "ns", None, today, on=date(2026, 9, 24))
        _call(db, "billed", day="2026-09-22")
        _call(db, "cleared", day="2026-09-25")
        assert _actual(db)["subscription_records"] == 1
        assert _actual(db)["external_unset_records"] == 1
    finally:
        db.close()


def test_oMLX_stays_local_and_is_never_billed(isolated_db, today):
    db = _db()
    try:
        _call(db, "a", model="omlx@local/qwen", cost=0.0)
        assert _actual(db) == {"subscription_records": 0, "unknown_charge_records": 0,
                               "direct_api_dollars": 0.0, "complete": True, "external_unset_records": 0}
    finally:
        db.close()


def test_an_external_call_for_a_server_with_no_row_is_unknown(isolated_db, today):
    db = _db()
    try:
        _call(db, "a", model="external@ns-gone/qwen3.8-flash")
        assert _actual(db)["unknown_charge_records"] == 1
    finally:
        db.close()


def test_a_removed_endpoint_keeps_its_mode_for_its_history(isolated_db, today):
    from llm import network_discovery as nd

    db = _db()
    try:
        _mode(db, "ns", "subscription", today, on=date(2026, 9, 5), cents=4000, day=21)
        db.commit()
        assert nd.remove_server("ns") is True
        _call(db, "a")
        assert _actual(db)["subscription_records"] == 1
    finally:
        db.close()


@pytest.mark.parametrize("kind, now, inside, outside", [
    # Days: the latest window is Sep 22 to Sep 28; Sep 29 is in progress and Sep 21 is the window before.
    ("days", datetime(2026, 9, 29, 12, tzinfo=timezone.utc), ("2026-09-22", "2026-09-28"), ("2026-09-29", "2026-09-21")),
    # Weeks: the latest four periods are Sep 14 to Oct 11; the period of Oct 12 is in progress.
    ("weeks", datetime(2026, 10, 12, 12, tzinfo=timezone.utc), ("2026-09-14", "2026-10-11"), ("2026-10-12", "2026-09-13")),
])
def test_actual_sums_the_latest_window_only(isolated_db, today, kind, now, inside, outside):
    db = _db()
    try:
        _mode(db, "ns", "pay_per_use", today, on=date(2026, 9, 5))
        for index, day in enumerate((*inside, *outside)):
            _call(db, f"direct{index}", model="openai/gpt-6.1", day=day, cost=1.0)
            _call(db, f"external{index}", day=day)
        params = {**PARAMS, "grain": resolve_grain(kind, "UTC", now=now)}
        result = build_actual(db, params)
        # The two rows inside the window count; the in-progress period and the window before do not.
        assert result["direct_api_dollars"] == pytest.approx(2.0 + 2 * 0.15)
        assert result["unknown_charge_records"] == 0
    finally:
        db.close()
