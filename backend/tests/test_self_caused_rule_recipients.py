"""A webhook marker suppresses only the rule recipient that made the call."""
import pytest

from events.connectors.github_admission import record_delivery_for_admission
from events.router import RouteOutcome, route_event
from events.self_caused import record_result
from tests.event_routing_helpers import register_pr
from tests.test_event_rules_2a import _agent, _project, _tool, _wakes, resolved, sent
from tools.mcp_stamp import ConnectedAppResult


@pytest.mark.asyncio
@pytest.mark.parametrize("mark_owner,other_rule", [(False, False), (False, True), (True, True)],
                         ids=["owner-only", "unmarked-owner", "marked-owner"])
async def test_marked_rule_recipient_does_not_wake(isolated_db, resolved, sent, mark_owner, other_rule):
    _project("proj-a")
    for thread in ("owner", "marked", "unmarked"):
        _agent(thread)
    register_pr("o/r", 7, "owner")
    for thread in (("marked", "unmarked") if other_rule else ("marked",)):
        rule = await _tool(thread, operation="create", connector="github", event_type="pr_comment.created",
                           match=[{"field": "repo", "op": "eq", "value": "o/r"}])
        assert isinstance(rule, dict), rule
    comment = ConnectedAppResult(
        '{"id":"123","url":"https://github.com/o/r/pull/7#issuecomment-123"}',
        {"mcp_app": "https://api.githubcopilot.com", "mcp_tool": "add_issue_comment"},
    )
    record_result("marked", comment)
    if mark_owner:
        record_result("owner", comment)
    payload = {"action": "created", "repository": {"full_name": "o/r"},
               "issue": {"number": 7, "pull_request": {"url": "pr"}},
               "comment": {"id": 123, "body": "hello", "user": {"login": "human"}},
               "sender": {"login": "human", "type": "User"}}
    record_delivery_for_admission("issue_comment", "created", "delivery", "o/r", payload)
    result = await route_event("webhook.github.pr_comment", payload, delivery_id="delivery")
    assert not _wakes("marked")
    assert len(_wakes("unmarked")) == int(other_rule)
    assert [thread for thread, _ in sent] == ([] if mark_owner else ["owner"])
    if mark_owner:
        assert result.outcome == RouteOutcome.SUPPRESSED


@pytest.mark.asyncio
async def test_marked_rule_recipient_without_routed_owner(isolated_db, resolved, sent):
    _project("proj-a")
    _agent("marked")
    rule = await _tool("marked", operation="create", connector="github", event_type="pr_comment.created",
                       match=[{"field": "repo", "op": "eq", "value": "o/r"}])
    assert isinstance(rule, dict), rule
    comment = ConnectedAppResult(
        '{"id":"123","url":"https://github.com/o/r/pull/7#issuecomment-123"}',
        {"mcp_app": "https://api.githubcopilot.com", "mcp_tool": "add_issue_comment"},
    )
    record_result("marked", comment)
    payload = {"action": "created", "repository": {"full_name": "o/r"},
               "issue": {"number": 7, "pull_request": {"url": "pr"}},
               "comment": {"id": 123, "body": "hello", "user": {"login": "human"}},
               "sender": {"login": "human", "type": "User"}}
    record_delivery_for_admission("issue_comment", "created", "delivery", "o/r", payload)
    await route_event("webhook.github.pr_comment", payload, delivery_id="delivery")
    assert not _wakes("marked")
    assert not sent
