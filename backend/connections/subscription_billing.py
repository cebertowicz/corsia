"""User-recorded billing for vendor accounts and External endpoints.

The store is ``vendor_account_billing_facts``: a payer's price is a history of
facts, each effective from its start date, so a later edit never rewrites a
past day. The first price applies back to the payer's creation; every later
edit starts on the day it is saved, and a second edit on that same day replaces
the first. Writes are guarded by the payer's own billing revision, never by the
account revision, so a sign-in that began before a price save still commits.
"""
from __future__ import annotations

from datetime import date, timedelta
import logging
import uuid

from connections.billing_prices import (
    OPEN_END,
    PRICE_KINDS,
    Charges,
    PriceFact,
    charges_in_range,
    fact_in_effect,
    mode_on,
    price_facts_by_payer,
)
from connections.harness_usage import allows_overage
from connections.vendor_accounts import AccountError, get_account
from infra.database import write_transaction
from infra.time import utc_now_iso

log = logging.getLogger(__name__)

MAX_PRICE_CENTS = 10_000_000
ENDPOINT_MODES = ("subscription", "pay_per_use")
ACCOUNT_STALE = "This account changed. Reload and try again."
ENDPOINT_STALE = "This endpoint changed. Reload and try again."


class BillingFactValidationError(AccountError):
    """A malformed billing request that the caller can correct in place."""


class BillingStale(AccountError):
    """The payer's billing changed after the caller read it."""

    code = "billing_changed"


class EndpointNotFound(AccountError):
    """The External endpoint a bill fact names has no row (a removed one keeps its row)."""

    code = "endpoint_not_found"


def _today() -> date:
    return date.fromisoformat(utc_now_iso()[:10])


def _civil(timestamp: str) -> date:
    return date.fromisoformat(timestamp[:10])


_UNREADABLE_LOGGED: set[str] = set()


def _existence(payer_id: str, created_at: str, removed_at: str | None) -> tuple[date, date | None] | None:
    """The civil dates a payer was created and removed, or None when its timestamps are unreadable.

    An unreadable payer has no existence to charge, so it must never read as
    covered: the callers count it as unrecorded. It is logged once per process.
    """
    try:
        return _civil(created_at), _civil(removed_at) if removed_at else None
    except ValueError:
        if payer_id not in _UNREADABLE_LOGGED:
            _UNREADABLE_LOGGED.add(payer_id)
            log.warning("billing payer %s has an unreadable created_at/removed_at; its charges are unrecorded", payer_id)
        return None


def _price(cents: object, day: object) -> tuple[int, int]:
    if type(cents) is not int or not 0 <= cents <= MAX_PRICE_CENTS:
        raise BillingFactValidationError("Enter a price of $0 or more with up to two decimals.")
    if type(day) is not int or not 1 <= day <= 31:
        raise BillingFactValidationError("Choose a day from 1 to 31.")
    return cents, day


def _revision(connection, column: str, payer_id: str) -> int:
    connection.execute(f"INSERT OR IGNORE INTO vendor_account_billing_revisions({column},revision) SELECT ?,0 "
                       f"WHERE NOT EXISTS (SELECT 1 FROM vendor_account_billing_revisions WHERE {column}=?)",
                       (payer_id, payer_id))
    return int(connection.execute(
        f"SELECT revision FROM vendor_account_billing_revisions WHERE {column}=?", (payer_id,)).fetchone()["revision"])


def _insert(connection, column: str, payer_id: str, *, kind: str, amount_cents: int, charge_day: int | None,
            start: date, end: str, supersedes_id: str | None) -> None:
    connection.execute(
        f"INSERT INTO vendor_account_billing_facts(id,{column},kind,amount_cents,start_date,end_date,charge_day,"
        "provenance,supersedes_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, payer_id, kind, amount_cents, start.isoformat(), end, charge_day,
         "user_recorded", supersedes_id, utc_now_iso()),
    )
    connection.execute(f"UPDATE vendor_account_billing_revisions SET revision=revision+1 WHERE {column}=?", (payer_id,))


def _append_price(connection, column: str, payer_id: str, created_on: date, *, kind: str, amount_cents: int,
                  charge_day: int | None, expected_revision: int, stale: str) -> None:
    """Append one effective-dated price fact after the billing revision check passes."""
    if type(expected_revision) is not int or expected_revision < 0:
        raise BillingFactValidationError("billing revision is invalid")
    if _revision(connection, column, payer_id) != expected_revision:
        raise BillingStale(stale)
    current = price_facts_by_payer(connection, column).get(payer_id, [])
    ever = connection.execute(
        f"SELECT 1 FROM vendor_account_billing_facts WHERE {column}=? AND kind IN ({','.join('?' * len(PRICE_KINDS))}) LIMIT 1",
        (payer_id, *PRICE_KINDS)).fetchone()
    start = _today() if ever else created_on
    same_day = next((fact for fact in current if fact.start == start), None)
    _insert(connection, column, payer_id, kind=kind, amount_cents=amount_cents, charge_day=charge_day,
            start=start, end=OPEN_END, supersedes_id=same_day.id if same_day else None)


def _account_for_write(connection, account_id: str) -> date:
    account = get_account(account_id, db=connection)
    if account["state"] == "removed":
        raise AccountError("A removed account's billing can no longer be changed.")
    return _civil(account["created_at"])


def _endpoint_row(db, server_id: str):
    row = db.execute("SELECT id,kind,status,created_at,updated_at FROM network_servers WHERE id=?",
                     (server_id,)).fetchone()
    if row is None:
        raise EndpointNotFound("endpoint does not exist")
    return row


def set_account_price(account_id: str, *, monthly_price_cents: int | None, charge_day: int | None,
                      expected_billing_revision: int, db=None) -> dict:
    """Record an account's monthly price and charge day, or clear both; returns its billing view."""
    if (monthly_price_cents is None) != (charge_day is None):
        raise BillingFactValidationError("Enter a price and a charge day together.")
    if monthly_price_cents is not None:
        _price(monthly_price_cents, charge_day)
    with write_transaction(db, scope="vendor_account_billing") as connection:
        created_on = _account_for_write(connection, account_id)
        if monthly_price_cents is None:
            current = fact_in_effect(price_facts_by_payer(connection, "account_id").get(account_id, []), _today())
            if current is None or current.kind == "price_cleared":
                if _revision(connection, "account_id", account_id) != expected_billing_revision:
                    raise BillingStale(ACCOUNT_STALE)
            else:
                _append_price(connection, "account_id", account_id, created_on, kind="price_cleared",
                              amount_cents=0, charge_day=None, expected_revision=expected_billing_revision,
                              stale=ACCOUNT_STALE)
        else:
            _append_price(connection, "account_id", account_id, created_on, kind="subscription_rate",
                          amount_cents=monthly_price_cents, charge_day=charge_day,
                          expected_revision=expected_billing_revision, stale=ACCOUNT_STALE)
        return account_billing(connection, [account_id])[account_id]


def set_endpoint_billing(server_id: str, *, mode: str | None, monthly_price_cents: int | None,
                         charge_day: int | None, expected_billing_revision: int, db=None) -> dict:
    """Record an External endpoint's mode (with a price for ``subscription``), or clear it; returns its billing view."""
    if mode not in (*ENDPOINT_MODES, None):
        raise BillingFactValidationError("Choose Subscription or Pay per use.")
    if mode == "subscription":
        _price(monthly_price_cents, charge_day)
    elif monthly_price_cents is not None or charge_day is not None:
        raise BillingFactValidationError("Only a subscription has a price and a charge day.")
    with write_transaction(db, scope="vendor_account_billing") as connection:
        row = _endpoint_row(connection, server_id)
        if row["kind"] != "external":
            raise BillingFactValidationError("Only an External endpoint has billing.")
        if row["status"] == "removed":
            raise AccountError("A removed endpoint's billing can no longer be changed.")
        created_on = _civil(row["created_at"])
        kind = {"subscription": "subscription_rate", "pay_per_use": "pay_per_use", None: "price_cleared"}[mode]
        current = mode_on(price_facts_by_payer(connection, "network_server_id").get(server_id, []), _today())
        if mode is None and current is None:
            if _revision(connection, "network_server_id", server_id) != expected_billing_revision:
                raise BillingStale(ENDPOINT_STALE)
        else:
            _append_price(connection, "network_server_id", server_id, created_on, kind=kind,
                          amount_cents=monthly_price_cents or 0, charge_day=charge_day,
                          expected_revision=expected_billing_revision, stale=ENDPOINT_STALE)
        return endpoint_billing(connection, [server_id])[server_id]


def record_overage(account_id: str, *, amount_cents: int, month: str, expected_billing_revision: int,
                   db=None) -> dict:
    """Record what an account paid past its plan limit for one calendar month; returns its billing view."""
    if type(amount_cents) is not int or not 0 <= amount_cents <= MAX_PRICE_CENTS:
        raise BillingFactValidationError("Enter an amount of $0 or more with up to two decimals.")
    try:
        first = date.fromisoformat(f"{month}-01")
    except (TypeError, ValueError) as error:
        raise BillingFactValidationError("Choose a month.") from error
    end = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    with write_transaction(db, scope="vendor_account_billing") as connection:
        _account_for_write(connection, account_id)
        if _revision(connection, "account_id", account_id) != expected_billing_revision:
            raise BillingStale(ACCOUNT_STALE)
        same = connection.execute(
            "SELECT f.id FROM vendor_account_billing_facts f WHERE f.account_id=? AND f.kind='overage_paid' "
            "AND f.start_date=? AND f.end_date=? "
            "AND NOT EXISTS (SELECT 1 FROM vendor_account_billing_facts r WHERE r.supersedes_id=f.id)",
            (account_id, first.isoformat(), end.isoformat())).fetchone()
        _insert(connection, "account_id", account_id, kind="overage_paid", amount_cents=amount_cents,
                charge_day=None, start=first, end=end.isoformat(), supersedes_id=same["id"] if same else None)
        return account_billing(connection, [account_id])[account_id]


def _revisions(db, column: str) -> dict[str, int]:
    return {str(row[0]): int(row[1]) for row in db.execute(
        f"SELECT {column},revision FROM vendor_account_billing_revisions WHERE {column} IS NOT NULL")}


def account_billing(db, account_ids: list[str] | None = None) -> dict[str, dict]:
    """Each account's price in effect today and its billing revision, read straight from the facts."""
    facts, revisions, today = price_facts_by_payer(db, "account_id"), _revisions(db, "account_id"), _today()
    ids = account_ids if account_ids is not None else [str(r[0]) for r in db.execute("SELECT id FROM vendor_accounts")]
    result = {}
    for account_id in ids:
        fact = fact_in_effect(facts.get(account_id, []), today)
        priced = fact is not None and fact.kind == "subscription_rate"
        result[account_id] = {
            "billing": {"monthly_price_cents": fact.amount_cents, "charge_day": fact.charge_day}
            if fact is not None and priced else None,
            "billing_revision": revisions.get(account_id, 0),
        }
    return result


def endpoint_billing(db, server_ids: list[str] | None = None) -> dict[str, dict]:
    """Each External endpoint's mode and price in effect today, and its billing revision."""
    facts, revisions, today = price_facts_by_payer(db, "network_server_id"), _revisions(db, "network_server_id"), _today()
    ids = server_ids if server_ids is not None else [str(r[0]) for r in db.execute("SELECT id FROM network_servers")]
    result = {}
    for server_id in ids:
        fact = fact_in_effect(facts.get(server_id, []), today)
        mode = mode_on(facts.get(server_id, []), today)
        result[server_id] = {
            "billing": None if fact is None or mode is None else {
                "mode": mode,
                "monthly_price_cents": fact.amount_cents if mode == "subscription" else None,
                "charge_day": fact.charge_day if mode == "subscription" else None},
            "billing_revision": revisions.get(server_id, 0),
        }
    return result


def _empty_context() -> dict:
    return {"subscription_dollars": None, "overage_dollars": None, "subscription_facts": 0, "overage_facts": 0,
            "subscription_unrecorded_accounts": 0, "subscription_coverage": "none", "overage_coverage": "none",
            "overlapping_overage": [], "overage_unrecorded": False, "allocation": "unavailable"}


def _coverage(intervals: list[tuple[date, date]], start: date, end: date) -> str:
    """Whether one account's recorded overage periods cover the range it existed in."""
    if not intervals:
        return "absent"
    cursor = start
    for interval_start, interval_end in sorted(intervals):
        if interval_end <= cursor:
            continue
        if interval_start > cursor:
            return "partial"
        cursor = max(cursor, interval_end)
        if cursor >= end:
            return "complete"
    return "partial"


def _bounds(from_ts: str, to_ts: str) -> tuple[date, date]:
    try:
        return date.fromisoformat(from_ts), date.fromisoformat(to_ts)
    except (TypeError, ValueError) as error:
        raise BillingFactValidationError("billing date must be an ISO civil date") from error


def subscription_charges(db, start: date, end: date) -> tuple[int, int, int, int]:
    """Cash charged in ``[start, end)`` by accounts and subscription endpoints.

    Returns total cents, payers with a recorded price, payers with an unrecorded
    day, and accounts that existed. Accounts count whenever they existed in the
    range; an endpoint counts only on days its mode was a subscription. A payer
    whose timestamps cannot be read counts as unrecorded.
    """
    accounts = price_facts_by_payer(db, "account_id")
    total = recorded = unrecorded_accounts = existed = 0
    for row in db.execute("SELECT id,created_at,removed_at FROM vendor_accounts"):
        span = _existence(str(row["id"]), row["created_at"], row["removed_at"])
        if span is None:
            existed += 1
            unrecorded_accounts += 1
            continue
        result: Charges = charges_in_range(accounts.get(str(row["id"]), []), span[0], span[1], start, end)
        if not result.existed:
            continue
        existed += 1
        total += result.cents
        if result.unrecorded:
            unrecorded_accounts += 1
        if not result.unrecorded or result.cents:
            recorded += 1
    endpoints = price_facts_by_payer(db, "network_server_id")
    for row in db.execute("SELECT id,status,created_at,updated_at FROM network_servers WHERE kind='external'"):
        facts: list[PriceFact] = endpoints.get(str(row["id"]), [])
        if not any(fact.kind == "subscription_rate" for fact in facts):
            continue
        span = _existence(str(row["id"]), row["created_at"], row["updated_at"] if row["status"] == "removed" else None)
        if span is None:
            unrecorded_accounts += 1
            continue
        result = charges_in_range(facts, span[0], span[1], start, end)
        if result.existed and result.cents:
            total += result.cents
            recorded += 1
    return total, recorded, unrecorded_accounts, existed


def workspace_context(db, *, from_ts: str, to_ts: str) -> dict:
    """Project recorded bills onto the Measurement window, never a scope allocation.

    ``from_ts``/``to_ts`` are ``core.measurement_scope.usage_bounds``' bare-date
    bounds, the one window rule. Subscription charges are cash-basis: a charge
    date inside the range counts the price in effect on it. The caller serves it
    at All work only (``core.measurement_scope.is_workspace_scope``).
    """
    context = _empty_context()
    start, end = _bounds(from_ts, to_ts)
    total, recorded, unrecorded_accounts, existed = subscription_charges(db, start, end)
    if recorded:
        context["subscription_dollars"] = total / 100
    context["subscription_facts"] = recorded
    context["subscription_unrecorded_accounts"] = unrecorded_accounts
    if existed or recorded:
        context["subscription_coverage"] = ("absent" if not recorded else
                                            "complete" if not unrecorded_accounts else "partial")
    intervals: dict[str, list[tuple[date, date]]] = {}
    overage_cents = overage_facts = 0
    for row in db.execute(
        "SELECT f.account_id,f.amount_cents,f.start_date,f.end_date FROM vendor_account_billing_facts f "
        "WHERE f.kind='overage_paid' AND f.account_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM vendor_account_billing_facts r WHERE r.supersedes_id=f.id) "
        "AND f.start_date < ? AND f.end_date > ? ORDER BY f.account_id,f.start_date,f.id",
        (end.isoformat(), start.isoformat()),
    ):
        fact_start, fact_end = date.fromisoformat(row["start_date"]), date.fromisoformat(row["end_date"])
        intervals.setdefault(str(row["account_id"]), []).append((max(start, fact_start), min(end, fact_end)))
        if start <= fact_start and fact_end <= end:
            overage_facts += 1
            overage_cents += int(row["amount_cents"])
        else:
            # A bill period the range only partly contains is shown as recorded,
            # never split across the range and never added to its total.
            context["overlapping_overage"].append({
                "amount_cents": int(row["amount_cents"]), "from_date": row["start_date"],
                "through": (fact_end - timedelta(days=1)).isoformat()})
    context["overage_dollars"] = overage_cents / 100 if overage_facts else None
    context["overage_facts"] = overage_facts
    accounts = db.execute("SELECT a.id,c.provider,a.created_at,a.removed_at FROM vendor_accounts a JOIN connections c ON c.id=a.connection_id").fetchall()
    required: dict[str, tuple[date, date]] = {}
    for account in accounts:
        span = _existence(str(account["id"]), account["created_at"], account["removed_at"])
        if span is None:
            required[account["id"]] = (start, end)
            continue
        existed_until = span[1] + timedelta(days=1) if span[1] else end
        needed = (max(start, span[0]), min(end, existed_until))
        if needed[0] < needed[1]:
            required[account["id"]] = needed
    states = [_coverage(intervals.get(account_id, []), s, e) for account_id, (s, e) in required.items()]
    if states:
        context["overage_coverage"] = ("absent" if all(state == "absent" for state in states) else
                                       "complete" if all(state == "complete" for state in states) else "partial")
    context["overage_unrecorded"] = (
        not overage_facts and not context["overlapping_overage"]
        and any(account["id"] in required and allows_overage(account["provider"], account["id"], db=db)
                for account in accounts))
    return context
