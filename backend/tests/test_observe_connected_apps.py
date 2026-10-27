"""Connected-app rules and source-event identity use the connector's declarations."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from events.self_caused import is_self_caused, record_result
from infra.database import get_db
from registry.connected_app_observation import notice_for_result
from tests.test_observe_rules import _agent, _owner, _tool, _write
from tools.mcp_stamp import ConnectedAppResult

APP = "https://api.githubcopilot.com"


def result(tool: str, text: str, app: str = APP) -> ConnectedAppResult:
    return ConnectedAppResult(text, {"mcp_app": app, "mcp_tool": tool})


@pytest.mark.asyncio
async def test_shipped_rule_and_agent_rule_use_connector_identity(isolated_db):
    _agent("a")
    _agent("b")
    made = await _tool("a", operation="create", kind="observe", tool="make_pr", item_type="pull-request")
    assert made["tool"] == "make_pr" and "argv_prefix" not in made
    assert "upstream tool name" in await _tool(
        "a", operation="create", kind="observe", tool="mcp__github__make_pr", item_type="pull-request")
    assert "no connector reports" in await _tool(
        "a", operation="create", kind="observe", tool="make_pr", item_type="unknown")
    output = '{"id":"91","url":"https://github.com/owner/repo/pull/7"}'
    assert notice_for_result("b", "turn-b", {}, result("create_pull_request", output)) == [
        "Registered pull request owner/repo#7 to you."]
    assert "already belongs to another chat" in notice_for_result("a", "turn-a", {}, result("make_pr", output))[0]
    assert _owner("owner/repo#7") == "b"
    assert notice_for_result("a", "turn-a", {}, result("make_pr", output, "https://wrong.example")) == []


@pytest.mark.asyncio
async def test_bridge_and_native_project_notice_from_in_memory_stamp(isolated_db):
    from core.turns_steps import _append_tool_results_sync
    from subagents.harness_tool_bridge import _with_notice_and_marker

    _agent("a")
    output = result("create_pull_request", '{"id":"3","url":"https://github.com/owner/repo/pull/7"}')
    with patch("core.plan_notice.plan_notice", return_value=None), \
            patch("core.repo_staleness_notice.repo_staleness_notice", return_value=None), \
            patch("events.connectors.github_pr_notice.pr_event_notice", return_value=None):
        text = await _with_notice_and_marker(
            str(output), SimpleNamespace(invocation_id="inv"), thread_id="a",
            turn_identity=SimpleNamespace(turn_id="turn"), provider_tool_call_id="call",
            name="alias", arguments={}, connected_app_result=output)
    assert "Registered pull request owner/repo#7 to you." in text
    output2 = result("create_pull_request", '{"id":"4","url":"https://github.com/owner/repo/pull/8"}')
    context = SimpleNamespace(add_tool_result=lambda messages, _id, _name, content: [*messages, {"role": "tool", "content": content}])
    call = SimpleNamespace(id="call", name="alias", arguments={})
    canonical = SimpleNamespace(turn_id="turn", media_metadata=None)
    projected = _append_tool_results_sync(SimpleNamespace(context=context), [], [(call, output2, canonical)], "a", None, None)
    assert "Registered pull request owner/repo#8 to you." in projected[0]["content"]


@pytest.mark.asyncio
async def test_comment_marked_without_observe_rule_and_routing_suppresses_only_caller(isolated_db, monkeypatch):
    from events.connectors.github_admission import record_delivery_for_admission
    from events.router import RouteOutcome, route_event

    _agent("a")
    _agent("b")
    pr = result("create_pull_request", '{"id":"7","url":"https://github.com/owner/repo/pull/7"}')
    notice_for_result("a", "turn", {}, pr)
    comment = result("add_issue_comment", '{"id":"123","url":"https://github.com/owner/repo/pull/7#issuecomment-123"}')
    record_result("a", comment)
    assert is_self_caused("github", "issue_comment:123", "a")
    assert notice_for_result("a", "turn", {}, comment) == []
    payload = {"action": "created", "repository": {"full_name": "owner/repo"},
               "issue": {"number": 7, "pull_request": {"url": "pr"}},
               "comment": {"id": 123, "body": "hello", "user": {"login": "human"}},
               "sender": {"login": "human", "type": "User"}}
    emitted = []
    monkeypatch.setattr("events.router.emit_product_event", lambda *a, **kw: emitted.append((a, kw)))
    record_delivery_for_admission("issue_comment", "created", "delivery-1", "owner/repo", payload)
    with patch("events.router.delivery.deliver_to_thread", return_value=True):
        first = await route_event("webhook.github.pr_comment", payload, delivery_id="delivery-1")
        assert first.outcome == RouteOutcome.SUPPRESSED and first.reason == "self_caused"
        other = {**payload, "corsia_tags": [{"thread_id": "b"}]}
        second = await route_event("webhook.github.pr_comment", other, delivery_id="delivery-1")
        assert second.outcome == RouteOutcome.ACCEPTED and second.thread_id == "b"
        assert any(args[:2] == ("event_route", "suppressed") and kw["properties"]["reason"] == "self_caused"
                   for args, kw in emitted)
        _write("UPDATE self_caused_events SET at='2000-01-01T00:00:00+00:00'")
        expired = await route_event("webhook.github.pr_comment", payload, delivery_id="delivery-1")
        assert expired.outcome == RouteOutcome.ACCEPTED and expired.thread_id == "a"
    db = get_db()
    try:
        assert db.execute("SELECT count(*) FROM event_route_decisions WHERE event_type='observe:self_caused'").fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_notifications_poller_suppresses_marked_comment(isolated_db):
    from events.connectors.github_notifications import _admit

    _agent("a")
    notice_for_result("a", "turn", {}, result("create_pull_request", '{"id":"7","url":"https://github.com/owner/repo/pull/7"}'))
    record_result("a", result("add_issue_comment", '{"id":"456","url":"https://github.com/owner/repo/pull/7#issuecomment-456"}'))
    payload = {"action": "created", "repository": {"full_name": "owner/repo"},
               "issue": {"number": 7, "pull_request": {"url": "pr"}},
               "comment": {"id": 456, "body": "hello", "user": {"login": "human"}},
               "sender": {"login": "human", "type": "User"}}
    with patch("events.router.delivery.deliver_to_thread", return_value=True) as delivered:
        assert await _admit("issue_comment", payload)
        delivered.assert_not_called()


@pytest.mark.asyncio
async def test_registry_marks_before_return_for_native_and_harness_paths(isolated_db, monkeypatch):
    from infra import state
    from tools.registry import ToolRegistry

    _agent("a")
    content = result("add_issue_comment", '{"id":"123","url":"https://github.com/owner/repo/pull/7#issuecomment-123"}')

    async def execute(*args, **kwargs):
        return content

    async def remind(text, *args):
        return text

    monkeypatch.setattr(ToolRegistry, "_execute_unredacted", execute)
    monkeypatch.setattr("tools.registry.remind_current_turn", remind)
    token = state._current_thread_id.set("a")
    try:
        await ToolRegistry().execute("alias", {})
        assert is_self_caused("github", "issue_comment:123", "a")
    finally:
        state._current_thread_id.reset(token)


def test_wrong_app_never_marks(isolated_db):
    _agent("a")
    output = '{"id":"123","url":"https://github.com/owner/repo/pull/7#issuecomment-123"}'
    record_result("a", result("add_issue_comment", output, "https://wrong.example"))
    assert not is_self_caused("github", "issue_comment:123", "a")
