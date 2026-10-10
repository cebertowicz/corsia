"""Effective-dated prices and the one charge-date helper: cash basis, clamped to month end."""
from __future__ import annotations

from datetime import date

from connections.billing_prices import PriceFact, charges_in_range, fact_in_effect, is_charge_date, mode_on


def _sub(start: str, cents: int, day: int, ident: str = "f") -> PriceFact:
    return PriceFact(ident, "subscription_rate", date.fromisoformat(start), cents, day)


def _d(value: str) -> date:
    return date.fromisoformat(value)


def test_a_charge_day_past_the_month_end_falls_on_the_last_day():
    assert is_charge_date(_d("2026-02-28"), 31) and not is_charge_date(_d("2026-02-27"), 31)
    assert is_charge_date(_d("2028-02-29"), 30) and is_charge_date(_d("2026-04-30"), 31)
    assert is_charge_date(_d("2026-03-31"), 31) and is_charge_date(_d("2026-10-14"), 14)


def test_a_range_counts_the_charge_dates_inside_it_with_an_inclusive_start_and_exclusive_end():
    facts = [_sub("2026-01-01", 2000, 14)]
    created = _d("2026-01-01")
    assert charges_in_range(facts, created, None, _d("2026-10-10"), _d("2026-10-21")).cents == 2000
    assert charges_in_range(facts, created, None, _d("2026-10-14"), _d("2026-10-15")).cents == 2000
    assert charges_in_range(facts, created, None, _d("2026-10-15"), _d("2026-11-14")).cents == 0
    assert charges_in_range(facts, created, None, _d("2026-10-15"), _d("2026-11-15")).cents == 2000
    assert charges_in_range(facts, created, None, _d("2026-10-01"), _d("2026-12-31")).cents == 6000


def test_a_month_end_charge_day_is_charged_once_every_month_including_short_ones():
    facts = [_sub("2026-01-01", 1000, 31)]
    result = charges_in_range(facts, _d("2026-01-01"), None, _d("2026-01-01"), _d("2026-05-01"))
    assert result.cents == 4000  # Jan 31, Feb 28, Mar 31, Apr 30


def test_charges_stop_at_removal_and_start_at_creation():
    facts = [_sub("2026-01-01", 1000, 10)]
    window = (_d("2026-01-01"), _d("2026-12-31"))
    assert charges_in_range(facts, _d("2026-03-11"), None, *window).cents == 9 * 1000  # Apr..Dec
    assert charges_in_range(facts, _d("2026-01-01"), _d("2026-03-10"), *window).cents == 3000  # removal day counts
    assert charges_in_range(facts, _d("2026-01-01"), _d("2026-03-09"), *window).cents == 2000
    assert not charges_in_range(facts, _d("2026-02-01"), _d("2026-02-15"), _d("2026-06-01"), _d("2026-07-01")).existed


def test_a_price_change_applies_from_its_date_and_leaves_earlier_charge_dates_alone():
    facts = [_sub("2026-01-01", 2000, 14, "a"), _sub("2026-10-10", 3000, 20, "b")]
    created = _d("2026-01-01")
    before = charges_in_range(facts, created, None, _d("2026-10-01"), _d("2026-10-10"))
    assert before.cents == 0  # the 14th is after the edit, so it is outside this range
    # Oct 14 falls after the edit, so the old charge day no longer applies; the new one is the 20th.
    assert charges_in_range(facts, created, None, _d("2026-10-01"), _d("2026-11-01")).cents == 3000
    assert charges_in_range(facts, created, None, _d("2026-09-01"), _d("2026-10-01")).cents == 2000


def test_a_charge_date_before_the_edit_keeps_the_old_price_and_one_after_uses_the_new_one():
    facts = [_sub("2026-01-01", 2000, 5, "a"), _sub("2026-10-10", 3000, 5, "b")]
    created = _d("2026-01-01")
    assert charges_in_range(facts, created, None, _d("2026-10-01"), _d("2026-10-10")).cents == 2000
    assert charges_in_range(facts, created, None, _d("2026-10-10"), _d("2026-11-10")).cents == 3000


def test_days_with_no_price_are_unrecorded_never_zero_dollars():
    created = _d("2026-01-01")
    assert charges_in_range([], created, None, _d("2026-10-01"), _d("2026-11-01")).unrecorded
    late = [_sub("2026-10-10", 2000, 14)]
    result = charges_in_range(late, created, None, _d("2026-10-01"), _d("2026-11-01"))
    assert result.unrecorded and result.cents == 2000  # days before the first price are unrecorded
    cleared = [_sub("2026-01-01", 2000, 14, "a"), PriceFact("c", "price_cleared", _d("2026-10-10"), 0, None)]
    after = charges_in_range(cleared, created, None, _d("2026-10-11"), _d("2026-11-30"))
    assert after.unrecorded and after.cents == 0


def test_a_recorded_zero_price_is_a_recorded_zero():
    result = charges_in_range([_sub("2026-01-01", 0, 14)], _d("2026-01-01"), None, _d("2026-10-01"), _d("2026-11-01"))
    assert result.cents == 0 and not result.unrecorded and result.existed


def test_the_mode_in_effect_follows_the_latest_fact_and_cleared_reads_unset():
    facts = [_sub("2026-01-01", 2000, 14, "a"), PriceFact("b", "pay_per_use", _d("2026-06-01"), 0, None),
             PriceFact("c", "price_cleared", _d("2026-09-01"), 0, None)]
    assert mode_on(facts, _d("2025-12-31")) is None
    assert mode_on(facts, _d("2026-05-31")) == "subscription"
    assert mode_on(facts, _d("2026-06-01")) == "pay_per_use"
    assert mode_on(facts, _d("2026-09-01")) is None
    assert fact_in_effect(facts, _d("2026-07-04")).id == "b"
