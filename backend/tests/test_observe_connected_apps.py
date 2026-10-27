"""Connected-app observation uses persisted identities and connector-owned JSON fields."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.tool_result_delivery import persist_tool_execution_result
from events.connectors.github_observe import extract
from infra.database import get_db
from registry.connected_app_observation import observe_persisted
from subagents.child_thread import persist_tool_use
from tests.test_observe_rules import _agent, _owner, _tool, _write
from tools.mcp_stamp import ConnectedAppResult

APP = "https://api.githubcopilot.com"


def persisted(thread: str, name: str, output: str, stamp: dict[str, str], suffix: str) -> str:
    db = get_db()
    call_id = f"call-{suffix}"
    step = persist_tool_use(thread, turn_id=f"turn-{suffix}", provider_turn_id=f"provider-{suffix}",
                            tool_id=call_id, name=name, arguments={}, db=db)
    result = persist_tool_execution_result(
        thread_id=thread, turn_id=f"turn-{suffix}", invocation_id=f"inv-{suffix}",
        assistant_step_id=step, provider_tool_call_id=call_id, name=name, arguments={},
        content=ConnectedAppResult(output, stamp) if stamp else output, is_error=False, db=db)
    db.close()
    return result.message_id


@pytest.mark.asyncio
async def test_persisted_stamp_registers_and_alias_alone_never_matches(isolated_db):
    _agent("a")
    from tools.post_update_reminder import REMINDER

    content = json.dumps({"number": 7, "base": {"repo": {"full_name": "owner/repo"}}}) + "\n\n" + REMINDER
    stamp = {"mcp_app": APP, "mcp_tool": "create_pull_request"}
    message = persisted("a", "mcp_alias", content, stamp, "one")
    assert observe_persisted("a", message, "turn-one", {}) == ["Registered pull request owner/repo#7 to you."]
    assert _owner("owner/repo#7") == "a"
    for suffix, wrong in (("unstamped", {}), ("wrong", {"mcp_app": "https://wrong.example", "mcp_tool": "create_pull_request"})):
        message = persisted("a", "mcp_alias", '{"number":8,"base":{"repo":{"full_name":"owner/repo"}}}', wrong, suffix)
        assert observe_persisted("a", message, f"turn-{suffix}", {}) == []
    assert _owner("owner/repo#8") is None


@pytest.mark.asyncio
async def test_model_receives_notice_after_stamped_result_is_persisted(isolated_db):
    from subagents.harness_tool_bridge import _with_notice_and_marker

    _agent("a")
    output = '{"id":3,"url":"https://github.com/owner/repo/pull/7"}'
    message = persisted("a", "alias", output, {"mcp_app": APP, "mcp_tool": "create_pull_request"}, "notice")
    with patch("core.plan_notice.plan_notice", return_value=None), \
            patch("core.repo_staleness_notice.repo_staleness_notice", return_value=None), \
            patch("events.connectors.github_pr_notice.pr_event_notice", return_value=None):
        text = await _with_notice_and_marker(
            output, SimpleNamespace(message_id=message, invocation_id="inv-notice"),
            thread_id="a", turn_identity=SimpleNamespace(turn_id="turn-notice"),
            provider_tool_call_id="call-notice", name="alias", arguments={}, connected_app=True)
    assert "Registered pull request owner/repo#7 to you." in text
    assert _owner("owner/repo#7") == "a"


def test_native_turn_receives_notice_after_persistence(isolated_db):
    from core.turns_steps import _append_tool_results_sync

    _agent("a")
    output = '{"id":3,"url":"https://github.com/owner/repo/pull/7"}'
    stamp = {"mcp_app": APP, "mcp_tool": "create_pull_request"}
    message = persisted("a", "alias", output, stamp, "native")
    context = SimpleNamespace(add_tool_result=lambda messages, _id, _name, result: [*messages, {"role": "tool", "content": result}])
    agent = SimpleNamespace(context=context)
    call = SimpleNamespace(id="call-native", name="alias", arguments={})
    canonical = SimpleNamespace(message_id=message, turn_id="turn-native", media_metadata=None)
    result = _append_tool_results_sync(agent, [], [(call, ConnectedAppResult(output, stamp), canonical)], "a", None, None)
    assert "Registered pull request owner/repo#7 to you." in result[0]["content"]
    assert _owner("owner/repo#7") == "a"


@pytest.mark.asyncio
async def test_create_and_other_owner(isolated_db):
    _agent("a")
    _agent("b")
    made = await _tool("a", operation="create", kind="observe", tool="make_pr", app=APP, item_type="pull-request")
    assert made["apps"] == [APP] and made["tool"] == "make_pr"
    alias = await _tool("a", operation="create", kind="observe", tool="mcp__github__make_pr", app=APP, item_type="pull-request")
    assert "upstream tool name" in alias
    refused = await _tool("a", operation="create", kind="observe", tool="make_pr", app=APP, item_type="unknown")
    assert "no connector reports" in refused
    output = '{"id":91,"url":"https://github.com/owner/repo/pull/7"}'
    first = persisted("b", "alias", output, {"mcp_app": APP, "mcp_tool": "create_pull_request"}, "first")
    observe_persisted("b", first, "turn-first", {})
    second = persisted("a", "alias", output, {"mcp_app": APP, "mcp_tool": "make_pr"}, "second")
    assert "already belongs to another chat" in observe_persisted("a", second, "turn-second", {})[0]
    assert _owner("owner/repo#7") == "b"


def test_connector_reads_only_typed_fields():
    full = extract('{"number":7,"base":{"repo":{"full_name":"o/r"}},"body":"https://github.com/x/y/pull/99"}', "create_pull_request", {})
    minimal = extract('{"id":3,"url":"https://github.com/o/r/pull/7"}', "create_pull_request", {})
    assert full.artifact.external_id == minimal.artifact.external_id == "o/r#7"
    assert extract('{"body":"https://github.com/x/y/pull/99"}', "create_pull_request", {}).artifact is None
    assert extract('not JSON https://github.com/x/y/pull/99', "create_pull_request", {}) is None
    assert extract('text before {"url":"https://github.com/o/r/pull/7"}', "create_pull_request", {}) is None
    assert extract('[{"url":"https://github.com/o/r/pull/7"}]', "create_pull_request", {}) is None
    assert extract('{"url":"https://[invalid"}', "create_pull_request", {}).artifact is None


@pytest.mark.asyncio
async def test_real_comment_route_skips_caller_for_webhook_and_poller_and_expires(isolated_db):
    from events import self_caused
    from events.connectors.github_webhook import dispatch_github_webhook_for_replay
    from events.router import RouteOutcome, route_event

    _agent("a")
    _agent("b")
    await _tool("a", operation="create", kind="observe", tool="add_issue_comment", app=APP, item_type="pull-request")
    pr = persisted("a", "alias", '{"number":7,"base":{"repo":{"full_name":"owner/repo"}}}',
                   {"mcp_app": APP, "mcp_tool": "create_pull_request"}, "pr")
    observe_persisted("a", pr, "turn-pr", {})
    output = '{"id":123,"url":"https://api.github.com/repos/owner/repo/issues/comments/123"}'
    msg = persisted("a", "alias", output, {"mcp_app": APP, "mcp_tool": "add_issue_comment"}, "comment")
    observe_persisted("a", msg, "turn-comment", {})
    payload = {"action": "created", "repository": {"full_name": "owner/repo"},
               "issue": {"number": 7, "pull_request": {"url": "pr"}},
               "comment": {"id": 123, "body": "hello", "user": {"login": "human"}},
               "sender": {"login": "human", "type": "User"}}
    with patch("events.router.delivery.deliver_to_thread", return_value=True) as delivered, \
            patch("events.router.dead_letter.park") as dead_letter:
        for copy in ("webhook", "poller"):
            event = await dispatch_github_webhook_for_replay("issue_comment", payload, delivery_id=copy)
            assert event == "webhook.github.pr_comment"
        assert delivered.call_count == 0
        dead_letter.assert_not_called()
        event_id = self_caused.identity("github", "issue_comment", "123")
        explained = await _tool("a", operation="explain", event_id=event_id)
        assert sum(row["reason"] == "self_caused" for row in explained["decisions"]) == 2
        # Route the same source event to another target using an explicit structured tag.
        other = {**payload, "corsia_tags": [{"thread_id": "b"}]}
        result = await route_event("webhook.github.pr_comment", other, delivery_id="other")
        assert result.outcome == RouteOutcome.ACCEPTED and result.thread_id == "b"
        event_id = self_caused.identity("github", "issue_comment", "123")
        explained = await _tool("a", operation="explain", event_id=event_id)
        assert any(row["reason"] == "self_caused" for row in explained["decisions"])
        _write("UPDATE event_route_decisions SET at='2000-01-01T00:00:00+00:00' WHERE event_type='observe:self_caused'")
        result = await route_event("webhook.github.pr_comment", payload, delivery_id="expired")
        assert result.outcome == RouteOutcome.ACCEPTED and result.thread_id == "a"
