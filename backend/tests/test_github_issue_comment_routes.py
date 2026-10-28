"""GitHub content route and default-wake boundaries."""
from types import SimpleNamespace

import pytest

from events.connectors.github_issue_activity import new_issues
from events.connectors.github_rules import _envelope
from events.github_mapper import map_github_event
from events.identity_formula import MalformedEventError
from events.router import _summarize


@pytest.mark.asyncio
async def test_issue_poller_and_webhook_use_the_same_mapper_and_facts(monkeypatch):
    monkeypatch.setattr("events.connectors.github_content_facts.is_own", lambda login: False)
    issue = {"number": 7, "created_at": "2026-10-25T00:00:00Z", "user": {"login": "author"}}
    payload = {"action": "opened", "repository": {"full_name": "o/r"}, "issue": issue,
               "sender": {"login": "author"}}
    deliveries = []

    async def get(*args):
        return SimpleNamespace(status=200, body=[issue])

    async def admit(*args):
        deliveries.append(args)

    assert await new_issues(get, admit, "o/r", {"issues_opened_at": "2026-10-24T00:00:00Z"}, None)
    poller_event, poller_payload, _ = deliveries[0]
    event = map_github_event("issues", payload)
    assert event == map_github_event(poller_event, poller_payload) == "webhook.github.issue_opened"
    a, b = _envelope(event, payload, None), _envelope(event, poller_payload, None)
    assert a.event_id == b.event_id and a.facts == b.facts
    assert a.facts["item_version"] == 7 and a.facts["author_origin"] == "outside"
    malformed = {**payload, "issue": {"user": {"login": "author"}}}
    with pytest.raises(MalformedEventError):
        _envelope(event, malformed, None)


@pytest.mark.parametrize("kind", ["comment", "pr", "issue"])
@pytest.mark.parametrize("origin", ["inside", "outside"])
def test_default_wake_omits_outside_author_text(monkeypatch, kind, origin):
    monkeypatch.setattr("events.connectors.github_content_facts.is_own", lambda login: login == "inside")
    item = {"number": 7, "id": 42, "user": {"login": origin}, "body": "BODY_CANARY",
            "title": "TITLE_CANARY", "html_url": "https://example.test/item"}
    payload = {"repository": {"full_name": "o/r"}, "action": "created" if kind == "comment" else "opened",
               "sender": {"login": "different"}}
    payload[{"pr": "pull_request", "issue": "issue", "comment": "comment"}[kind]] = item
    summary, details = _summarize("webhook.github.test", payload, "o/r#7")
    combined = repr((summary, details))
    assert details["author_origin"] == origin and details["author"] == origin
    assert details["repo"] == "o/r" and details["external_id"] == "o/r#7"
    assert details["url"] == "https://example.test/item" and details["action"] == payload["action"]
    if origin == "outside" or kind == "issue":
        assert "BODY_CANARY" not in combined and "TITLE_CANARY" not in combined
    elif kind == "comment":
        assert "BODY_CANARY" in combined
    else:
        assert "TITLE_CANARY" in combined


def test_malformed_event_refuses_rule_with_visible_reason(monkeypatch):
    from events.connectors import github_rules

    warnings = []
    monkeypatch.setattr(github_rules.rule_reads, "type_rules", lambda *args: (True, False))
    monkeypatch.setattr(github_rules.logger, "warning", lambda *args: warnings.append(args))
    raw = {"action": "opened", "repository": {"full_name": "o/r"}, "issue": {}}
    scope = github_rules.rule_scope("webhook.github.issue_opened", raw, None)
    assert not scope.any_rule and scope.envelope is None
    assert warnings and "no rule evaluated" in warnings[0][0]


def test_missing_author_and_failed_lookup_do_not_expose_text(monkeypatch):
    from events.connectors import github_content_facts

    raw = {"repository": {"full_name": "o/r"}, "action": "created",
           "comment": {"body": "BODY_CANARY", "user": {}}}
    assert "BODY_CANARY" not in repr(_summarize("event", raw, "o/r#7"))

    def fail(_payload):
        raise RuntimeError("secret content must not be logged")

    monkeypatch.setattr(github_content_facts, "content_facts", fail)
    assert "BODY_CANARY" not in repr(_summarize("event", raw, "o/r#7"))


def test_no_author_text_requires_no_origin_read(monkeypatch):
    from events.connectors import github_content_facts

    def fail(_payload):
        raise AssertionError("unexpected origin read")

    monkeypatch.setattr(github_content_facts, "content_facts", fail)
    summary, _ = _summarize("event", {"action": "opened", "issue": {"number": 7}}, "o/r#7")
    assert "o/r#7" in summary


def test_review_types_normalize_but_are_not_routed(monkeypatch):
    from events.envelope import normalize

    monkeypatch.setattr("events.connectors.github_content_facts.is_own", lambda login: False)
    for action in ("submitted", "edited"):
        payload = {"action": action, "repository": {"full_name": "o/r"},
                   "pull_request": {"number": 7}, "review": {"id": 42,
                   "updated_at": "2026-10-25T00:00:00Z", "user": {"login": "outside"}}}
        assert normalize("github", payload).type == f"review.{action}"
        assert map_github_event("pull_request_review", payload) is None
        if action == "edited":
            del payload["review"]["updated_at"]
            with pytest.raises(MalformedEventError):
                normalize("github", payload)
