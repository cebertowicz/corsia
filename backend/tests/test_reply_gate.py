"""Reply routes use the shared rule-driven read gate."""
import pytest

from events.connectors import github_pr_facts, github_route_preparation, github_rules
from tests.test_github_pr_facts import _rule
from tests.test_github_reply_facts import payload


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,routed,event", [
    ("issue", "webhook.github.pr_comment", "pr_comment.created"),
    ("line", "webhook.github.pr_comment", "pr_comment.created"),
    ("review", "webhook.github.review_submitted", "review.submitted"),
])
@pytest.mark.parametrize("field,expected", [("author", False), ("check_state", True)])
async def test_reply_rule_controls_read(isolated_db, monkeypatch, kind, routed, event, field, expected):
    _rule(event, [{"field": field, "op": "present"}])
    raw = payload(kind)
    scope = github_rules.rule_scope(routed, raw, None)
    called = []

    async def enrich(envelope):
        called.append(envelope)
        return envelope

    monkeypatch.setattr(github_rules, "rule_scope", lambda *_args: scope)
    monkeypatch.setattr(github_pr_facts, "enriched", enrich)
    monkeypatch.setattr(github_route_preparation, "_prepare_route", lambda *_args: None)
    await github_route_preparation.prepare_route_ordered(routed, raw, None, None)
    assert bool(called) is expected
