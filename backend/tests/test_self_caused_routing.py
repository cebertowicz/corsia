"""Self-caused delivery retains other recipients and reports marker errors."""
from unittest.mock import patch

import pytest

from events.connectors.github_admission import record_delivery_for_admission
from events.router import RouteOutcome, route_event
from events.self_caused import record_result
from infra import state
from tests.event_routing_helpers import register_pr
from tests.test_event_rules_2a import _agent, _project, _rows, _tool, _wakes, resolved, sent
from tools.mcp_stamp import ConnectedAppResult, result_stamp
from tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_self_caused_comment_keeps_rule_recipient_and_passive_change(isolated_db, resolved, sent):
    _project("proj-a")
    _agent("owner")
    _agent("watcher")
    register_pr("o/r", 7, "owner")
    rule = await _tool("watcher", operation="create", connector="github", event_type="pr_comment.created",
                       match=[{"field": "repo", "op": "eq", "value": "o/r"}])
    assert isinstance(rule, dict), rule
    comment = ConnectedAppResult('{"id":"123","url":"https://github.com/o/r/pull/7#issuecomment-123"}',
                                 {"mcp_app": "https://api.githubcopilot.com", "mcp_tool": "add_issue_comment"})
    record_result("owner", comment)
    payload = {"action": "created", "repository": {"full_name": "o/r"},
               "issue": {"number": 7, "pull_request": {"url": "pr"}},
               "comment": {"id": 123, "body": "hello", "user": {"login": "human"}},
               "sender": {"login": "human", "type": "User"}}
    record_delivery_for_admission("issue_comment", "created", "review-delivery", "o/r", payload)
    result = await route_event("webhook.github.pr_comment", payload, delivery_id="review-delivery")
    assert (result.outcome, result.thread_id, result.reason) == (RouteOutcome.SUPPRESSED, "owner", "self_caused")
    assert sent == []
    assert len(_wakes("watcher")) == 1
    changes = _rows("SELECT * FROM context_change_journal WHERE object_kind='artifact' AND change_kind=?",
                    "webhook.github.pr_comment")
    assert len(changes) == 1 and changes[0]["actor_thread_id"] == "owner"


@pytest.mark.asyncio
async def test_marker_failure_is_visible_on_stamped_tool_result(isolated_db, monkeypatch):
    result = ConnectedAppResult("completed", {"mcp_app": "app", "mcp_tool": "tool"})
    async def execute(*args, **kwargs):
        return result
    async def remind(text, *args):
        return text
    monkeypatch.setattr(ToolRegistry, "_execute_unredacted", execute)
    monkeypatch.setattr("tools.registry.remind_current_turn", remind)
    token = state._current_thread_id.set("owner")
    try:
        with patch("events.self_caused.record_result", side_effect=RuntimeError("write failed")):
            returned = await ToolRegistry().execute("alias", {})
    finally:
        state._current_thread_id.reset(token)
    assert "completed" in returned and "Couldn't record" in returned and "may wake you again" in returned
    assert result_stamp(returned) == result_stamp(result)
