"""Recorded billing: effective-dated prices on accounts and endpoints, and Actual's use of them."""
from __future__ import annotations

import asyncio
from datetime import date

import pytest
from fastapi import HTTPException

from connections import subscription_billing as sb
from connections.models import ConnectionStatus, ConnectionType, upsert_connection
from connections.vendor_accounts import get_account
from infra.database import get_db
from llm import network_discovery as nd
from vendor_account_fixtures import signed_in_default

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login

PARAMS = {"scope_project": "all", "scope_agent": None, "user_id": "steve", "from": "2026-09-01", "to": "2026-10-31"}
CREATED = "2026-09-01T00:00:00Z"


@pytest.fixture
def today(monkeypatch):
    """Pin the UTC civil date the store stamps on a new fact."""
    state = {"day": date(2026, 9, 1)}
    monkeypatch.setattr(sb, "_today", lambda: state["day"])
    return state


def _account(db, provider: str = "codex") -> dict:
    upsert_connection(type=ConnectionType.CLI, provider=provider, status=ConnectionStatus.AVAILABLE,
                      config={"binary": f"/bin/{provider}"}, db=db)
    account = signed_in_default(provider, db=db)
    # The fixture database seeds placeholder accounts; every account here existed from CREATED.
    db.execute("UPDATE vendor_accounts SET created_at=?", (CREATED,))
    return account


def _solo(db) -> dict:
    """The one account a test is about: any other seeded account is removed from the database."""
    account = _account(db)
    db.execute("DELETE FROM vendor_accounts WHERE id != ?", (account["id"],))
    return account


def _set(db, account_id: str, cents, day):
    revision = sb.account_billing(db, [account_id])[account_id]["billing_revision"]
    return sb.set_account_price(account_id, monthly_price_cents=cents, charge_day=day,
                                expected_billing_revision=revision, db=db)


def _actual_subscription(db, *, start: str, end: str) -> dict:
    return sb.workspace_context(db, from_ts=start, to_ts=end)


def _facts(db, account_id: str):
    return db.execute("SELECT kind,amount_cents,start_date,charge_day,supersedes_id FROM vendor_account_billing_facts "
                      "WHERE account_id=? ORDER BY created_at,rowid", (account_id,)).fetchall()


def test_the_first_price_applies_back_to_the_account_creation_date(isolated_db, today):
    db = get_db()
    try:
        account = _solo(db)
        today["day"] = date(2026, 10, 20)
        view = _set(db, account["id"], 2000, 14)
        assert view["billing"] == {"monthly_price_cents": 2000, "charge_day": 14}
        assert [row["start_date"] for row in _facts(db, account["id"])] == ["2026-09-01"]
        assert _actual_subscription(db, start="2026-09-01", end="2026-10-31")["subscription_dollars"] == 40.0
    finally:
        db.close()


def test_a_later_edit_starts_on_the_save_date_and_leaves_past_charge_dates_alone(isolated_db, today):
    db = get_db()
    try:
        account = _solo(db)
        _set(db, account["id"], 2000, 14)
        today["day"] = date(2026, 10, 10)
        _set(db, account["id"], 3000, 14)
        assert [row["start_date"] for row in _facts(db, account["id"])] == ["2026-09-01", "2026-10-10"]
        # September 14 keeps $20; October 14 (after the edit) uses $30.
        assert _actual_subscription(db, start="2026-09-01", end="2026-10-01")["subscription_dollars"] == 20.0
        assert _actual_subscription(db, start="2026-10-01", end="2026-11-01")["subscription_dollars"] == 30.0
        # A range that ends before the edit never sees the new price.
        assert _actual_subscription(db, start="2026-09-01", end="2026-10-10")["subscription_dollars"] == 20.0
    finally:
        db.close()


def test_a_second_edit_on_the_same_day_supersedes_the_first(isolated_db, today):
    db = get_db()
    try:
        account = _solo(db)
        _set(db, account["id"], 2000, 14)
        today["day"] = date(2026, 10, 10)
        _set(db, account["id"], 3000, 14)
        _set(db, account["id"], 3500, 20)
        rows = _facts(db, account["id"])
        assert len(rows) == 3 and rows[2]["supersedes_id"] is not None
        assert sb.account_billing(db, [account["id"]])[account["id"]]["billing"] == {
            "monthly_price_cents": 3500, "charge_day": 20}
        assert _actual_subscription(db, start="2026-10-01", end="2026-11-01")["subscription_dollars"] == 35.0
    finally:
        db.close()


def test_clearing_returns_the_account_to_not_recorded_for_later_days_only(isolated_db, today):
    db = get_db()
    try:
        account = _solo(db)
        _set(db, account["id"], 2000, 14)
        today["day"] = date(2026, 10, 10)
        view = _set(db, account["id"], None, None)
        assert view["billing"] is None
        assert _actual_subscription(db, start="2026-09-01", end="2026-10-01")["subscription_dollars"] == 20.0
        later = _actual_subscription(db, start="2026-10-10", end="2026-12-01")
        assert later["subscription_dollars"] is None and later["subscription_unrecorded_accounts"] == 1
        assert _set(db, account["id"], None, None)["billing"] is None  # clearing an unset price is a no-op
    finally:
        db.close()


def test_an_unset_price_reads_not_recorded_and_never_zero(isolated_db, today):
    db = get_db()
    try:
        _solo(db)
        context = _actual_subscription(db, start="2026-09-01", end="2026-10-01")
        assert context["subscription_dollars"] is None
        assert context["subscription_coverage"] == "absent" and context["subscription_unrecorded_accounts"] == 1
    finally:
        db.close()


def test_a_recorded_zero_price_is_zero_dollars_and_recorded(isolated_db, today):
    db = get_db()
    try:
        account = _solo(db)
        _set(db, account["id"], 0, 14)
        context = _actual_subscription(db, start="2026-09-01", end="2026-10-01")
        assert context["subscription_dollars"] == 0.0 and context["subscription_coverage"] == "complete"
    finally:
        db.close()


def test_one_account_unrecorded_among_recorded_ones_makes_the_total_partial(isolated_db, today):
    db = get_db()
    try:
        recorded, other = _account(db, "codex"), _account(db, "claude-code")
        _set(db, recorded["id"], 2000, 14)
        context = _actual_subscription(db, start="2026-09-01", end="2026-10-01")
        assert context["subscription_dollars"] == 20.0 and context["subscription_unrecorded_accounts"] == 1
        assert context["subscription_coverage"] == "partial" and other["id"] != recorded["id"]
    finally:
        db.close()


def test_a_charge_date_stops_at_the_removal_of_the_account(isolated_db, today):
    db = get_db()
    try:
        account = _solo(db)
        _set(db, account["id"], 2000, 14)
        db.execute("UPDATE vendor_accounts SET state='removed', removed_at='2026-09-20T00:00:00Z' WHERE id=?",
                   (account["id"],))
        assert _actual_subscription(db, start="2026-09-01", end="2026-12-01")["subscription_dollars"] == 20.0
        with pytest.raises(sb.AccountError):
            _set(db, account["id"], 3000, 14)
    finally:
        db.close()


def test_a_stale_billing_revision_is_refused_and_a_pair_is_required(isolated_db, today):
    db = get_db()
    try:
        account = _solo(db)
        _set(db, account["id"], 2000, 14)
        with pytest.raises(sb.BillingStale, match="This account changed. Reload and try again."):
            sb.set_account_price(account["id"], monthly_price_cents=3000, charge_day=14,
                                 expected_billing_revision=0, db=db)
        for cents, day, message in ((-1, 14, "Enter a price"), (10_000_001, 14, "Enter a price"),
                                    (2000, 0, "Choose a day"), (2000, 32, "Choose a day"), (True, 14, "Enter a price")):
            with pytest.raises(sb.BillingFactValidationError, match=message):
                sb.set_account_price(account["id"], monthly_price_cents=cents, charge_day=day,
                                     expected_billing_revision=1, db=db)
        with pytest.raises(sb.BillingFactValidationError):
            sb.set_account_price(account["id"], monthly_price_cents=2000, charge_day=None,
                                 expected_billing_revision=1, db=db)
        assert len(_facts(db, account["id"])) == 1
    finally:
        db.close()


def test_saving_a_price_never_changes_the_account_revision_so_a_sign_in_still_commits(isolated_db, today):
    from connections.account_signin_commit import commit_signed_in

    db = get_db()
    try:
        account = _solo(db)
        started_at = get_account(account["id"], db=db)["revision"]
        _set(db, account["id"], 2000, 14)
        _set(db, account["id"], 2500, 14)
        assert get_account(account["id"], db=db)["revision"] == started_at
        db.commit()
    finally:
        db.close()
    commit_signed_in(account["id"], started_at, {"email": "user@example.com"})
    assert get_account(account["id"])["state"] == "signed_in"


def test_overage_is_recorded_for_a_calendar_month_and_re_adding_the_month_replaces_it(isolated_db, today):
    db = get_db()
    try:
        account = _solo(db)
        revision = sb.account_billing(db, [account["id"]])[account["id"]]["billing_revision"]
        sb.record_overage(account["id"], amount_cents=1250, month="2026-09", expected_billing_revision=revision, db=db)
        sb.record_overage(account["id"], amount_cents=1500, month="2026-09", expected_billing_revision=revision + 1, db=db)
        context = _actual_subscription(db, start="2026-09-01", end="2026-10-01")
        assert context["overage_dollars"] == 15.0 and context["overage_facts"] == 1
        with pytest.raises(sb.BillingFactValidationError):
            sb.record_overage(account["id"], amount_cents=1, month="September", expected_billing_revision=2, db=db)
    finally:
        db.close()


def test_overage_is_flagged_unrecorded_only_when_an_account_spends_past_its_plan(isolated_db, today):
    db = get_db()
    try:
        account = _solo(db)
        assert _actual_subscription(db, start="2026-09-01", end="2026-10-01")["overage_unrecorded"] is False
        db.execute("UPDATE vendor_accounts SET usage_json='{\"allow_overage\": true}' WHERE id=?", (account["id"],))
        assert _actual_subscription(db, start="2026-09-01", end="2026-10-01")["overage_unrecorded"] is True
        sb.record_overage(account["id"], amount_cents=100, month="2026-09", expected_billing_revision=0, db=db)
        assert _actual_subscription(db, start="2026-09-01", end="2026-10-01")["overage_unrecorded"] is False
    finally:
        db.close()


def test_the_account_routes_return_the_billing_and_map_stale_and_invalid_writes(isolated_db, today):
    from api import vendor_accounts as api

    db = get_db()
    try:
        account = _solo(db)
        db.commit()
    finally:
        db.close()
    listed = asyncio.run(api.list_vendor_accounts())["accounts"][0]
    assert listed["billing"] is None and listed["billing_revision"] == 0
    saved = asyncio.run(api.set_vendor_account_billing(
        account["id"], api.AccountPrice(monthly_price_cents=2000, charge_day=14, expected_billing_revision=0)))
    assert saved["billing"] == {"monthly_price_cents": 2000, "charge_day": 14} and saved["billing_revision"] == 1
    assert asyncio.run(api.list_vendor_accounts())["accounts"][0]["billing"] == saved["billing"]
    stale = asyncio.run(api.set_vendor_account_billing(
        account["id"], api.AccountPrice(monthly_price_cents=3000, charge_day=14, expected_billing_revision=0)))
    assert stale.status_code == 409 and b"This account changed. Reload and try again." in stale.body
    with pytest.raises(HTTPException) as invalid:
        asyncio.run(api.set_vendor_account_billing(
            account["id"], api.AccountPrice(monthly_price_cents=3000, charge_day=40, expected_billing_revision=1)))
    assert invalid.value.status_code == 422


# ---- External endpoints -------------------------------------------------------------------------


def _server(instance: str = "plan.example.com:443/v1") -> str:
    server = nd.upsert_server("external", instance, "Plan", "plan.example.com", 443, "online", kind="external")
    db = get_db()
    db.execute("UPDATE network_servers SET created_at=? WHERE id=?", (CREATED, server))
    db.commit()
    return server


def _endpoint(db, server: str, mode, cents=None, day=None):
    revision = sb.endpoint_billing(db, [server])[server]["billing_revision"]
    return sb.set_endpoint_billing(server, mode=mode, monthly_price_cents=cents, charge_day=day,
                                   expected_billing_revision=revision, db=db)


def test_an_endpoint_mode_is_recorded_from_its_creation_and_switches_from_the_save_date(isolated_db, today):
    server = _server()
    db = get_db()
    try:
        assert sb.endpoint_billing(db, [server])[server]["billing"] is None
        today["day"] = date(2026, 10, 10)
        _endpoint(db, server, "pay_per_use")
        today["day"] = date(2026, 10, 20)
        view = _endpoint(db, server, "subscription", 4000, 21)
        assert view["billing"] == {"mode": "subscription", "monthly_price_cents": 4000, "charge_day": 21}
        facts = db.execute("SELECT kind,start_date FROM vendor_account_billing_facts WHERE network_server_id=? "
                           "ORDER BY created_at,rowid", (server,)).fetchall()
        assert [(row["kind"], row["start_date"]) for row in facts] == [
            ("pay_per_use", "2026-09-01"), ("subscription_rate", "2026-10-20")]
        assert _endpoint(db, server, "pay_per_use")["billing"] == {
            "mode": "pay_per_use", "monthly_price_cents": None, "charge_day": None}
        assert _endpoint(db, server, None)["billing"] is None
    finally:
        db.close()


def test_endpoint_writes_validate_the_mode_and_pair_and_refuse_a_stale_revision(isolated_db, today):
    server = _server()
    db = get_db()
    try:
        for mode, cents, day in (("free", None, None), ("subscription", None, None), ("subscription", 4000, None),
                                 ("pay_per_use", 4000, 21), (None, None, 3)):
            with pytest.raises(sb.BillingFactValidationError):
                sb.set_endpoint_billing(server, mode=mode, monthly_price_cents=cents, charge_day=day,
                                        expected_billing_revision=0, db=db)
        _endpoint(db, server, "pay_per_use")
        with pytest.raises(sb.BillingStale, match="This endpoint changed. Reload and try again."):
            sb.set_endpoint_billing(server, mode="subscription", monthly_price_cents=1, charge_day=1,
                                    expected_billing_revision=0, db=db)
        with pytest.raises(sb.EndpointNotFound):
            sb.set_endpoint_billing("ns-missing", mode=None, monthly_price_cents=None, charge_day=None,
                                    expected_billing_revision=0, db=db)
    finally:
        db.close()


def test_the_payer_check_refuses_a_fact_with_two_payers_or_none(isolated_db, today):
    import sqlite3

    server = _server()
    db = get_db()
    try:
        account = _solo(db)
        insert = ("INSERT INTO vendor_account_billing_facts(id,account_id,network_server_id,kind,amount_cents,start_date,"
                  "end_date,charge_day,created_at) VALUES (?,?,?,?,?,?,?,?,?)")
        row = ("subscription_rate", 1, "2026-09-01", "9999-12-31", 5, "t")
        for args in (("x1", None, None, *row), ("x2", account["id"], server, *row)):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(insert, args)
        with pytest.raises(sqlite3.IntegrityError):  # a subscription rate needs its charge day
            db.execute(insert, ("x3", account["id"], None, "subscription_rate", 1, "2026-09-01", "9999-12-31", None, "t"))
    finally:
        db.close()


def test_remove_and_re_add_keeps_the_id_and_the_billing_history(isolated_db, today):
    server = _server()
    db = get_db()
    try:
        _endpoint(db, server, "subscription", 4000, 21)
        db.commit()
    finally:
        db.close()
    assert nd.remove_server(server) is True
    assert nd.get_server(server)["status"] == "removed"
    again = _server()
    assert again == server and nd.get_server(server)["status"] == "online"
    db = get_db()
    try:
        assert sb.endpoint_billing(db, [again])[again]["billing"] == {
            "mode": "subscription", "monthly_price_cents": 4000, "charge_day": 21}
    finally:
        db.close()


def test_a_subscription_endpoint_charges_on_its_charge_dates(isolated_db, today):
    server = _server()
    db = get_db()
    try:
        db.execute("UPDATE vendor_accounts SET created_at=?", (CREATED,))
        _endpoint(db, server, "subscription", 4000, 21)
        context = _actual_subscription(db, start="2026-09-01", end="2026-11-01")
        assert context["subscription_dollars"] == 80.0
    finally:
        db.close()


def test_the_server_routes_expose_billing_and_write_it_without_touching_metadata(isolated_db, today):
    from api import network_server_billing as route
    from api.network_servers import list_servers

    server = _server()
    nd.set_server_metadata(server, cache_warm_seconds=300)
    listed = asyncio.run(list_servers())["servers"][0]
    assert listed["billing"] is None and listed["billing_revision"] == 0 and listed["list_priced"] is False
    saved = asyncio.run(route.put_billing(server, route.EndpointBillingWrite(
        mode="subscription", monthly_price_cents=4000, charge_day=21, expected_billing_revision=0)))
    assert saved["billing"]["mode"] == "subscription" and saved["billing_revision"] == 1
    assert asyncio.run(list_servers())["servers"][0]["billing"] == saved["billing"]
    assert nd.get_server_metadata(server)["cache_warm_seconds"] == 300 and "billing_mode" not in nd.get_server_metadata(server)
    stale = asyncio.run(route.put_billing(server, route.EndpointBillingWrite(mode=None, expected_billing_revision=0)))
    assert stale.status_code == 409
    with pytest.raises(HTTPException) as bad:
        asyncio.run(route.put_billing(server, route.EndpointBillingWrite(mode="free", expected_billing_revision=1)))
    assert bad.value.status_code == 422
    with pytest.raises(HTTPException) as gone:
        asyncio.run(route.put_billing("ns-missing", route.EndpointBillingWrite(mode=None, expected_billing_revision=0)))
    assert gone.value.status_code == 404


def test_a_payer_with_unreadable_timestamps_is_logged_once_and_never_reads_complete(isolated_db, today, monkeypatch, caplog):
    monkeypatch.setattr(sb, "_UNREADABLE_LOGGED", set())
    db = get_db()
    try:
        priced = _solo(db)
        _set(db, priced["id"], 2000, 1)
        broken = _account(db, "claude-code")
        db.execute("UPDATE vendor_accounts SET created_at='not-a-date' WHERE id=?", (broken["id"],))
        db.execute("DELETE FROM vendor_accounts WHERE id NOT IN (?,?)", (priced["id"], broken["id"]))
        sb.record_overage(priced["id"], amount_cents=500, month="2026-09", expected_billing_revision=1, db=db)
        with caplog.at_level("WARNING", logger=sb.__name__):
            first = _actual_subscription(db, start="2026-09-01", end="2026-10-01")
            second = _actual_subscription(db, start="2026-09-01", end="2026-10-01")
        assert first == second
        assert first["subscription_unrecorded_accounts"] == 1 and first["subscription_coverage"] == "partial"
        # The recorded account is complete for the month; the unreadable one counts as absent, so overage is partial.
        assert first["overage_coverage"] == "partial"
        message = f"billing payer {broken['id']} has an unreadable created_at/removed_at; its charges are unrecorded"
        assert [record.getMessage() for record in caplog.records].count(message) == 1
    finally:
        db.close()


def test_an_unreadable_payer_alone_reads_unrecorded_not_complete(isolated_db, today, monkeypatch):
    monkeypatch.setattr(sb, "_UNREADABLE_LOGGED", set())
    db = get_db()
    try:
        account = _solo(db)
        db.execute("UPDATE vendor_accounts SET created_at='not-a-date' WHERE id=?", (account["id"],))
        context = _actual_subscription(db, start="2026-09-01", end="2026-10-01")
        assert context["subscription_unrecorded_accounts"] == 1 and context["subscription_coverage"] == "absent"
        assert context["subscription_dollars"] is None and context["overage_coverage"] == "absent"
    finally:
        db.close()


def test_overage_only_partly_inside_the_range_is_listed_and_never_added_to_the_total(isolated_db, today):
    db = get_db()
    try:
        account = _solo(db)
        sb.record_overage(account["id"], amount_cents=1250, month="2026-09", expected_billing_revision=0, db=db)
        partial = _actual_subscription(db, start="2026-09-28", end="2026-10-05")
        sb.record_overage(account["id"], amount_cents=300, month="2026-10", expected_billing_revision=1, db=db)
        both = _actual_subscription(db, start="2026-09-15", end="2026-11-01")
    finally:
        db.close()
    listed = {"amount_cents": 1250, "from_date": "2026-09-01", "through": "2026-09-30"}
    assert partial["overage_dollars"] is None and partial["overage_facts"] == 0
    assert partial["overlapping_overage"] == [listed]
    assert both["overage_dollars"] == 3.0 and both["overage_facts"] == 1
    assert both["overlapping_overage"] == [listed]


def test_overage_needs_no_facts_before_the_account_existed(isolated_db, today):
    db = get_db()
    try:
        account = _solo(db)
        db.execute("UPDATE vendor_accounts SET created_at='2026-10-02T09:00:00Z' WHERE id=?", (account["id"],))
        sb.record_overage(account["id"], amount_cents=0, month="2026-10", expected_billing_revision=0, db=db)
        created_later = _actual_subscription(db, start="2026-09-28", end="2026-10-08")
        db.execute("UPDATE vendor_accounts SET created_at=? WHERE id=?", (CREATED, account["id"]))
        existed_all_along = _actual_subscription(db, start="2026-09-28", end="2026-10-08")
    finally:
        db.close()
    assert created_later["overage_coverage"] == "complete"
    assert existed_all_along["overage_coverage"] == "partial"
