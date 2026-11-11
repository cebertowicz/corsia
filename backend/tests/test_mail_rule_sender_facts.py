"""Mail rules can name a sender's bare address or domain, on Gmail, Outlook and IMAP alike,
and an agent's own mail rule fires on new mail in a conversation no agent owns yet."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from connections.connected_accounts import create_account
from connections.mail import email_screen
from connections.microsoft import mail
from connections.microsoft.provider import MICROSOFT
from events.connectors import get_manifest, mail_poller as poller
from events.rule_match import matches
from events.rule_outcomes import Rule
from infra.database import get_db
from tests.event_routing_helpers import mk_session
from tools.event_rules import EventRulesTool

PROVIDERS = {"google": "gmail", "microsoft": "microsoft", "mailhost": "mailhost"}
CLEAN = email_screen.Screen("screened", "mail", "later", False, "none", "local")
UNSCREENED = email_screen.Screen("unavailable")


def _message(sender: str, message_id: str = "m1") -> mail.Message:
    return mail.Message(message_id, f"conversation-{message_id}",
                        {"from": sender, "subject": "invoice", "date": "2026-11-10T00:00:00Z"}, "body")


def _payload(sender: str, provider: str = "google", screen: email_screen.Screen = CLEAN) -> dict:
    return poller.event_payload("acct-1", _message(sender), screen, "position", provider)


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
@pytest.mark.parametrize("screen", [CLEAN, UNSCREENED])
def test_every_message_carries_the_senders_bare_address_and_domain(provider, screen):
    facts = get_manifest(PROVIDERS[provider]).extract_facts(
        _payload("Billing Team <Billing@Em.Acme.com>", provider, screen), None)
    assert facts["from_address"] == "billing@em.acme.com"
    assert facts["from_domain"] == "em.acme.com"
    # The header itself is unchanged: its display name shows only after a clean screen.
    expected = "Billing Team <Billing@Em.Acme.com>" if screen is CLEAN else "Billing@Em.Acme.com"
    assert facts["from"] == expected


def test_a_sender_with_no_plain_address_gets_neither_fact():
    facts = _payload("Undisclosed <not an address>")
    assert "from_address" not in facts and "from_domain" not in facts


def _match_rule(connector: str, *tests: dict) -> Rule:
    return Rule("r1", "project:p", connector, "email.received", tests, "deliver", "thread:a",
                "wake", None, "a")


@pytest.mark.parametrize("connector", sorted(PROVIDERS.values()))
@pytest.mark.parametrize(("domain", "fires"), [
    ("acme.com", True), ("em.acme.com", True), ("ACME.COM", True),
    ("mail.acme.com", False), ("cme.com", False),
])
def test_a_domain_test_matches_that_domain_and_its_subdomains(connector, domain, fires):
    facts = get_manifest(connector).extract_facts(_payload("Billing <billing@em.acme.com>"), None)
    assert matches(_match_rule(connector, {"field": "from_domain", "op": "eq", "value": domain}),
                   facts) is fires


def test_a_lookalike_domain_never_matches():
    facts = get_manifest("gmail").extract_facts(_payload("x@notacme.com"), None)
    domain = {"field": "from_domain", "value": "acme.com"}
    assert not matches(_match_rule("gmail", {**domain, "op": "eq"}), facts)
    assert matches(_match_rule("gmail", {**domain, "op": "not_eq"}), facts)
    assert matches(_match_rule("gmail", {"field": "from_domain", "op": "in",
                                         "value": ["other.org", "notacme.com"]}), facts)


def test_an_address_test_ignores_case_and_display_name_but_from_stays_exact():
    facts = get_manifest("gmail").extract_facts(_payload("Billing Team <billing@acme.com>"), None)
    assert matches(_match_rule("gmail", {"field": "from_address", "op": "eq",
                                         "value": "Billing@ACME.com"}), facts)
    assert not matches(_match_rule("gmail", {"field": "from", "op": "eq",
                                             "value": "billing@acme.com"}), facts)


# -- End to end: new mail in a conversation no agent owns, through the poller ------------------

@pytest.fixture
def sent():
    with patch("events.delivery.send_thread_input", AsyncMock()):
        yield


@pytest.fixture(autouse=True)
def _no_analytics(monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")


def _rows(sql: str, *args) -> list[dict]:
    db = get_db()
    try:
        return [dict(row) for row in db.execute(sql, args).fetchall()]
    finally:
        db.close()


def _write(sql: str, *args) -> None:
    db = get_db()
    try:
        db.execute(sql, args)
        db.commit()
    finally:
        db.close()


def _agent(thread_id: str, project: str) -> None:
    if not _rows("SELECT 1 FROM projects WHERE id=?", project):
        now = datetime.now(timezone.utc).isoformat()
        _write("INSERT INTO projects (id, name, created_at, updated_at) VALUES (?,?,?,?)",
               project, project, now, now)
    mk_session(thread_id, project_id=project)
    _write("UPDATE threads SET agent_enabled=1 WHERE id=?", thread_id)


def _account(*projects: str) -> str:
    mode = "selected" if projects else "all"
    reach = {"mode": mode, "project_ids": list(projects), "outside_projects": False}
    return str(create_account(MICROSOFT.name, "person@example.test", reach=reach,
                              created_by=("system", "test"), account_kind="personal",
                              provider_subject="tenant:object")["id"])


def _rule(thread_id: str, field: str, value: str) -> None:
    out = asyncio.run(EventRulesTool().execute(
        _thread_id=thread_id, operation="create", connector="microsoft", event_type="email.received",
        match=[{"field": field, "op": "eq", "value": value}], mode="wake"))
    assert not out.startswith(("Refused:", "Error:")), out


def _mail(account: str, sender: str, message_id: str) -> None:
    payload = poller.event_payload(account, _message(sender, message_id), CLEAN, "position", "microsoft")
    assert poller._deliver(account, payload)


def _wakes(thread_id: str) -> list[str]:
    return [row["content"] for row in _rows(
        "SELECT content FROM messages WHERE thread_id=? AND id LIKE 'platform-event:%'", thread_id)]


def _no_owner_parked() -> list[dict]:
    return _rows("SELECT reason FROM dead_letter_events WHERE reason='no_owner'")


def test_an_exact_address_self_rule_wakes_its_writer_for_a_new_conversation(isolated_db, sent):
    _agent("finance", "p")
    account = _account()
    _rule("finance", "from_address", "billing@acme.com")
    _mail(account, "Billing Team <Billing@Acme.com>", "m1")
    assert len(_wakes("finance")) == 1
    assert _no_owner_parked() == []


def test_two_writers_matching_self_rules_are_each_woken_once(isolated_db, sent):
    _agent("finance", "p")
    _agent("ops", "q")
    account = _account()
    _rule("finance", "from_address", "billing@acme.com")
    _rule("ops", "from_domain", "acme.com")
    _mail(account, "billing@acme.com", "m1")
    assert (len(_wakes("finance")), len(_wakes("ops"))) == (1, 1)


def test_a_writer_outside_the_accounts_reach_is_not_woken(isolated_db, sent):
    _agent("finance", "p")
    _agent("outsider", "q")
    account = _account("p")
    _rule("finance", "from_domain", "acme.com")
    _rule("outsider", "from_domain", "acme.com")
    _mail(account, "billing@acme.com", "m1")
    assert (len(_wakes("finance")), _wakes("outsider")) == (1, [])


def test_the_reach_filter_still_holds_a_writer_the_scope_lets_through(isolated_db, sent, monkeypatch):
    _agent("finance", "p")
    _agent("outsider", "q")
    account = _account("p")
    monkeypatch.setattr("connections.connected_accounts.reached_projects",
                        lambda _account, db: frozenset({"p", "q"}))
    _rule("outsider", "from_domain", "acme.com")
    _mail(account, "billing@acme.com", "m1")
    assert _wakes("outsider") == []
    [held] = _rows("SELECT thread_id, outcome, reason FROM event_route_decisions WHERE via='rule'")
    assert (held["thread_id"], held["outcome"], held["reason"]) == (
        "outsider", "held", "account_out_of_reach")


def test_an_unreadable_reach_wakes_no_one_and_records_nothing_against_any_rule(
        isolated_db, sent, monkeypatch):
    _agent("finance", "p")
    _agent("outsider", "q")
    account = _account("p")
    _rule("finance", "from_domain", "acme.com")
    _rule("outsider", "from_domain", "acme.com")
    monkeypatch.setattr("connections.connected_accounts.reached_projects",
                        lambda _account, db: None)
    _mail(account, "billing@acme.com", "m1")
    assert (_wakes("finance"), _wakes("outsider")) == ([], [])
    assert _rows("SELECT 1 FROM event_route_decisions WHERE via='rule'") == []
    assert len(_no_owner_parked()) == 1  # the mail still parks: no one took it


def test_a_domain_rule_wakes_for_its_subdomains_and_never_a_lookalike(isolated_db, sent):
    _agent("finance", "p")
    account = _account()
    _rule("finance", "from_domain", "acme.com")
    _mail(account, "Billing <billing@em.acme.com>", "m1")
    _mail(account, "x@notacme.com", "m2")
    assert len(_wakes("finance")) == 1
    assert len(_no_owner_parked()) == 1  # the lookalike's mail still parks: no one took it
