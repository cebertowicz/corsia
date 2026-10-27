"""Outside-content classification and fact boundaries."""
from __future__ import annotations

import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from connections.mail import email_screen
from events import guard
from events.connectors.github import MANIFEST
from events.envelope import normalize
from events.github_mapper import map_github_event
from events.connectors.mail_poll_read import event_payload
from tools.email_read import EmailSearchTool
from tools.microsoft_actions import MicrosoftMailSearchTool


@pytest.fixture(autouse=True)
def identities(monkeypatch):
    monkeypatch.setattr("events.connectors.github_content_facts.is_own", lambda login: login == "inside")
    monkeypatch.setattr(guard, "_configured_self_authors", lambda: {"inside"})


def item(login="outside"):
    return {"id": 42, "number": 7, "user": {"login": login}, "body": "BODY_CANARY", "title": "TITLE_CANARY", "html_url": "https://example.test/item", "updated_at": "2026-10-26T00:00:00Z", "created_at": "2026-10-25T00:00:00Z"}


def payload(kind, action="opened", login="outside"):
    raw = {"repository": {"full_name": "o/r"}, "action": action, "sender": {"login": "inside"}}
    if kind == "pr":
        raw["pull_request"] = {**item(login), "head": {"sha": "head123"}, "merged": False}
    elif kind == "issue":
        raw["issue"] = item(login)
    elif kind == "review":
        raw["pull_request"] = {**item("inside"), "head": {"sha": "head123"}}
        raw["review"] = item(login)
    else:
        raw["issue"] = {**item("inside"), **({"pull_request": {"url": "x"}} if kind == "pr_comment" else {})}
        raw["comment"] = item(login)
    return raw


@pytest.mark.parametrize("kind,action,expected", [
    ("pr", "opened", "pr.opened"), ("pr", "synchronize", "pr.synchronize"),
    ("pr", "ready_for_review", "pr.ready_for_review"), ("pr", "labeled", "pr.labeled"),
    ("pr", "closed", "pr.closed"),
    ("issue", "opened", "issue.opened"), ("issue", "edited", "issue.edited"),
    ("issue_comment", "created", "issue_comment.created"), ("issue_comment", "edited", "issue_comment.edited"),
    ("pr_comment", "created", "pr_comment.created"), ("pr_comment", "edited", "pr_comment.edited"),
    ("review", "submitted", "review.submitted"), ("review", "edited", "review.edited"),
])
def test_content_author_and_versions(kind, action, expected):
    raw = payload(kind, action)
    assert MANIFEST.classify(raw) == expected
    facts = MANIFEST.extract_facts(raw, None)
    assert facts["author"] == "outside" and facts["author_origin"] == "outside"
    assert "BODY_CANARY" not in json.dumps(facts) and "TITLE_CANARY" not in json.dumps(facts)
    version = "head123" if kind == "pr" and action in ("opened", "synchronize") else "42:2026-10-26T00:00:00Z" if action == "edited" else 7 if action == "opened" else 42
    assert facts["item_version"] == version
    if action == "edited" or kind == "review":
        event = {"pr_comment": "issue_comment", "issue_comment": "issue_comment", "issue": "issues", "review": "pull_request_review"}[kind]
        assert map_github_event(event, raw) is None


def test_unknown_and_suppression_share_author():
    raw = payload("issue_comment", "created", "inside")
    raw["sender"] = {"login": "outside"}
    assert guard.is_bot_or_self_authored(raw)
    assert MANIFEST.extract_facts(raw, None)["author_origin"] == "inside"
    raw["comment"]["user"] = {}
    assert not guard.is_bot_or_self_authored(raw)
    assert MANIFEST.extract_facts(raw, None)["author_origin"] == "outside"


def test_own_sender_acting_on_outside_pr_is_not_suppressed():
    raw = payload("pr", "labeled")
    assert raw["sender"]["login"] == "inside"
    assert not guard.is_bot_or_self_authored(raw)
    assert MANIFEST.extract_facts(raw, None)["author_origin"] == "outside"


@pytest.mark.parametrize("login,expected_calls", [("outside", 1), ("inside", 0)])
def test_identity_view_once_per_delivery(monkeypatch, login, expected_calls):
    from connections import github_agent
    from events.connectors import github_content_facts, github_ownership, github_rules
    from events.connectors.github_route_preparation import PreparedRoute

    monkeypatch.setattr(github_content_facts, "is_own", github_ownership.is_own)
    monkeypatch.setattr(github_ownership, "cached_own_login", lambda: "inside")
    view = Mock(return_value=SimpleNamespace(ready=False))
    monkeypatch.setattr(github_agent, "view", view)
    monkeypatch.setattr(github_agent, "agent_login", lambda account: None)
    monkeypatch.setattr(github_rules.rule_reads, "type_rules", lambda *args: (True, True))
    monkeypatch.setattr(github_rules.rule_reads, "unrouted_projects", lambda *args, **kwargs: frozenset({"p"}))
    monkeypatch.setattr(github_rules, "apply_webhook_rules_sync", lambda *args, **kwargs: None)
    raw = payload("pr", "opened", login)
    scope = github_rules.rule_scope("webhook.github.pr_opened", raw, None)
    github_rules.apply_routed_rules(PreparedRoute(thread_id="owner"), "webhook.github.pr_opened", raw, None, {}, scope=scope)
    assert view.call_count == expected_calls


def test_normalized_outside_pr_and_comment_contain_no_text():
    for kind, action in (("pr", "opened"), ("pr_comment", "created")):
        event = normalize("github", payload(kind, action))
        serialized = json.dumps(asdict(event))
        assert "BODY_CANARY" not in serialized and "TITLE_CANARY" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,event_type,action", [("pr", "pull_request", "opened"), ("pr_comment", "issue_comment", "created")])
async def test_existing_webhook_route_normalizes_outside_content(main_db, monkeypatch, kind, event_type, action):
    from events.connectors.github_rules import RuleScope
    from events.connectors.github_webhook import dispatch_github_webhook_for_replay
    from tests.event_routing_helpers import mk_session, register_pr

    mk_session("outside-owner")
    register_pr("o/r", 7, "outside-owner")
    captured = []
    monkeypatch.setattr("events.connectors.github_rules.rule_scope", lambda *args: RuleScope())
    monkeypatch.setattr("events.connectors.github_rules.apply_webhook_rules_sync", lambda envelope, *args, **kwargs: captured.append(envelope))
    monkeypatch.setattr("events.delivery.deliver_and_resolve_to_thread", AsyncMock(return_value=True))
    raw = payload(kind, action)
    await dispatch_github_webhook_for_replay(event_type, raw, delivery_id=f"test-{kind}")
    assert len(captured) == 1
    facts = captured[0].facts
    assert facts["author_origin"] == "outside"
    assert "BODY_CANARY" not in json.dumps(facts) and "TITLE_CANARY" not in json.dumps(facts)


@pytest.mark.asyncio
# The declaration case's second read is dev's existing PR-author check in
# github_ownership.declaration_facts, not a second origin lookup.
@pytest.mark.parametrize("login,expected_calls,has_declaration", [("outside", 1, False), ("inside", 0, False), ("outside", 2, True)])
async def test_delivery_identity_reads_by_author_and_declaration(main_db, monkeypatch, login, expected_calls, has_declaration):
    from connections import github_agent
    from events.connectors import github_content_facts, github_ownership, github_rules
    from events.connectors.github_webhook import dispatch_github_webhook_for_replay
    from tests.event_routing_helpers import mk_session, register_pr

    mk_session("count-owner")
    register_pr("o/r", 7, "count-owner")
    monkeypatch.setattr(guard, "_configured_self_authors", lambda: set())
    monkeypatch.setattr(github_content_facts, "is_own", github_ownership.is_own)
    monkeypatch.setattr(github_ownership, "cached_own_login", lambda: "inside")
    view = Mock(return_value=SimpleNamespace(ready=False))
    monkeypatch.setattr(github_agent, "view", view)
    monkeypatch.setattr(github_agent, "agent_login", lambda account: None)
    monkeypatch.setattr(github_rules.rule_reads, "type_rules", lambda *args: (True, True))
    monkeypatch.setattr(github_rules.rule_reads, "unrouted_projects", lambda *args, **kwargs: frozenset({"p"}))
    monkeypatch.setattr(github_rules, "apply_webhook_rules_sync", lambda *args, **kwargs: None)
    deliver = AsyncMock(return_value=True)
    monkeypatch.setattr("events.delivery.deliver_and_resolve_to_thread", deliver)
    raw = payload("pr", "opened", login)
    if has_declaration:
        raw["pull_request"]["body"] = "Owner thread ID: 12345678"
    raw["sender"] = {"login": "different-sender"}
    await dispatch_github_webhook_for_replay("pull_request", raw, delivery_id=f"count-{login}")
    assert deliver.await_count == 1
    assert view.call_count == expected_calls


@pytest.mark.asyncio
async def test_flagged_sender_is_address_on_event_and_search(monkeypatch):
    flagged = email_screen.Screen("screened", injection=True, rule="R1")
    message = SimpleNamespace(message_id="m1", thread_id="t1", headers={"from": "ATTACKER NAME <outside@example.org>", "date": ""})
    for provider in ("google", "microsoft"):
        event = event_payload("a", message, flagged, "1", provider)
        connector = "gmail" if provider == "google" else "microsoft"
        facts = normalize(connector, event).facts
        assert facts["from"] == "outside@example.org" and facts["author_origin"] == "outside"
    assert email_screen.sender_for_screen(message.headers["from"], None) == "outside@example.org"
    assert email_screen.sender_for_screen(message.headers["from"], email_screen.Screen("screened")) == message.headers["from"]
    assert email_screen.sender_for_screen("ATTACKER NAME", flagged) == ""
    async def reader(_kwargs):
        return "a", SimpleNamespace(search=AsyncMock(return_value=[message]))
    async def db_call(fn, *args):
        return {"m1": flagged}
    monkeypatch.setattr("tools.email_read._reader", reader)
    monkeypatch.setattr("tools.email_read.run_in_db", db_call)
    google = await EmailSearchTool().execute(query="x")
    monkeypatch.setattr("tools.microsoft_actions._account", AsyncMock(return_value="a"))
    monkeypatch.setattr("tools.microsoft_actions.mail.search", AsyncMock(return_value=[message]))
    monkeypatch.setattr("tools.microsoft_actions.run_in_db", db_call)
    microsoft = await MicrosoftMailSearchTool().execute(query="x", account_id="a")
    for result in (google, microsoft):
        assert "ATTACKER NAME" not in result
        assert json.loads(result)["messages"][0]["from"] == "outside@example.org"
