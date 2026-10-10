"""Effective-dated prices: which one applies on a day, and what it charges in a range.

A payer (a vendor account or an External endpoint) has a history of price facts,
each effective from its ``start_date`` until the next one. The price in effect on a
day is the latest current fact that started on or before it, so editing a price
never rewrites an earlier day. Charges are counted on a cash basis: one price per
charge date, never prorated, and the charge day is clamped to the month's last day.
There is no cached copy of any result; every reader derives it from the facts.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

#: End date stored on a fact that stays in effect until a later one replaces it.
OPEN_END = "9999-12-31"
#: The kinds that decide a payer's price (and an endpoint's mode); overage bills do not.
PRICE_KINDS = ("subscription_rate", "pay_per_use", "price_cleared")


@dataclass(frozen=True, slots=True)
class PriceFact:
    """One current (non-superseded) price-kind fact."""

    id: str
    kind: str
    start: date
    amount_cents: int
    charge_day: int | None


@dataclass(frozen=True, slots=True)
class Charges:
    """What a payer charged in a range, and whether any of its days had no recorded price."""

    cents: int
    unrecorded: bool
    existed: bool


def price_facts_by_payer(db, payer_column: str) -> dict[str, list[PriceFact]]:
    """Every payer's current price facts in one read, each list ordered by effective date."""
    if payer_column not in ("account_id", "network_server_id"):
        raise ValueError("unknown payer column")
    kinds = ",".join("?" * len(PRICE_KINDS))
    facts: dict[str, list[PriceFact]] = {}
    for row in db.execute(
        f"SELECT f.id,f.{payer_column} AS payer,f.kind,f.amount_cents,f.start_date,f.charge_day "
        f"FROM vendor_account_billing_facts f WHERE f.{payer_column} IS NOT NULL AND f.kind IN ({kinds}) "
        "AND NOT EXISTS (SELECT 1 FROM vendor_account_billing_facts r WHERE r.supersedes_id=f.id) "
        "ORDER BY f.start_date,f.created_at,f.rowid",
        PRICE_KINDS,
    ):
        facts.setdefault(str(row["payer"]), []).append(PriceFact(
            str(row["id"]), str(row["kind"]), date.fromisoformat(row["start_date"]),
            int(row["amount_cents"]), row["charge_day"]))
    return facts


def fact_in_effect(facts: list[PriceFact], day: date) -> PriceFact | None:
    """The latest fact that started on or before ``day``; None before the first one."""
    found = None
    for fact in facts:
        if fact.start > day:
            break
        found = fact
    return found


def mode_on(facts: list[PriceFact], day: date) -> str | None:
    """An endpoint's mode on ``day``: ``subscription``, ``pay_per_use``, or None when unset."""
    fact = fact_in_effect(facts, day)
    if fact is None or fact.kind == "price_cleared":
        return None
    return "subscription" if fact.kind == "subscription_rate" else "pay_per_use"


def is_charge_date(day: date, charge_day: int) -> bool:
    """Whether ``day`` is the charge date of a month, clamping 29-31 to the month's last day."""
    return day.day == min(charge_day, calendar.monthrange(day.year, day.month)[1])


def charges_in_range(facts: list[PriceFact], created_on: date, removed_on: date | None,
                     start: date, end: date) -> Charges:
    """Cash charged in ``[start, end)`` while the payer existed, using the price in effect on each charge date.

    The payer exists from the civil date it was created through the civil date it was
    removed. A day with no price fact, or after the price was cleared, is unrecorded;
    a ``pay_per_use`` day charges nothing here because its usage is priced per call.
    """
    first = max(start, created_on)
    last = end if removed_on is None else min(end, removed_on + timedelta(days=1))
    if first >= last:
        return Charges(0, False, False)
    cents, unrecorded = 0, False
    day = first
    while day < last:
        fact = fact_in_effect(facts, day)
        if fact is None or fact.kind == "price_cleared":
            unrecorded = True
        elif fact.kind == "subscription_rate" and fact.charge_day is not None and is_charge_date(day, fact.charge_day):
            cents += fact.amount_cents
        day += timedelta(days=1)
    return Charges(cents, unrecorded, True)
