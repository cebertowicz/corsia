"""The explicit agent-to-user update tool."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from delegation_fixtures import deleg_db  # noqa: F401
from infra.thread_input_types import TurnIdentity

TURN = TurnIdentity(turn_id="turn-1", turn_order=1)


def _own_node(db, thread_id: str, title: str = "Current task"):
    from plans import service

    plan = service.ensure_plan("thread", thread_id, db=db)
    node = service.add_node(
        plan.id,
        kind="task",
        title=title,
        acceptance_criteria="Done",
        db=db,
    )
    db.commit()
    return node


@pytest.mark.asyncio
async def test_post_update_writes_trimmed_agent_message_and_broadcasts(deleg_db, monkeypatch):
    from tools.post_update import PostUpdateTool

    node = _own_node(deleg_db, "sender-self")
    broadcast = AsyncMock()
    monkeypatch.setattr("tools.post_update.broadcast_message_rows", broadcast)

    result = await PostUpdateTool().execute(
        _thread_id="sender-self", text="  The focused check passed.  ", node_id=node.id,
        _host_access_turn_identity=TURN,
    )

    assert result.startswith("Update saved as message ")
    row = deleg_db.execute(
        "SELECT role, content, metadata FROM messages WHERE thread_id='sender-self'"
    ).fetchone()
    assert row["role"] == "assistant"
    assert row["content"] == "The focused check passed."
    assert json.loads(row["metadata"]) == {"for_user": True, "turn_id": "turn-1", "node_id": node.id}
    broadcast.assert_awaited_once_with("sender-self", (result.removeprefix("Update saved as message ").removesuffix("."),))


@pytest.mark.asyncio
async def test_post_update_reaches_a_live_thread_without_reload(deleg_db, monkeypatch):
    """The real broadcast path (unmocked) must publish the update, not silently drop it."""
    from infra import state
    from tools.post_update import PostUpdateTool

    events: list[dict] = []
    monkeypatch.setattr(
        state, "broadcast_to_thread", lambda _thread_id, payload: events.append(payload),
    )

    result = await PostUpdateTool().execute(
        _thread_id="sender-self", text="Live update", _host_access_turn_identity=TURN,
    )

    assert result.startswith("Update saved as message ")
    assert events, "post_update's message must reach a live thread immediately, not only after reload"
    message = events[0]["message"]
    assert message["role"] == "assistant"
    assert message["content"] == "Live update"


@pytest.mark.asyncio
async def test_post_update_without_a_calling_turn_refuses_and_writes_nothing(deleg_db):
    from tools.post_update import PostUpdateTool

    result = await PostUpdateTool().execute(_thread_id="sender-self", text="Orphan")

    assert result.startswith("Error: the calling Turn could not be resolved")
    assert deleg_db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_post_update_rejects_empty_and_too_long_text_without_writing(deleg_db):
    from tools.post_update import PostUpdateTool

    tool = PostUpdateTool()
    assert tool.parameters["properties"]["text"]["minLength"] == 1
    assert await tool.execute(_thread_id="sender-self", text=" \n ") == (
        "Error: text must be non-empty after trimming"
    )
    assert await tool.execute(_thread_id="sender-self", text="x" * 2_001) == (
        "Error: text must be at most 2,000 characters"
    )
    assert deleg_db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_post_update_rejects_a_foreign_plan_node_without_writing(deleg_db):
    from tools.post_update import PostUpdateTool

    deleg_db.execute(
        "INSERT INTO threads (id,user_id,model,title,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        ("other-thread", "u-test", "claude-code/opus", "Other", "t", "t"),
    )
    foreign = _own_node(deleg_db, "other-thread", "Foreign task")

    result = await PostUpdateTool().execute(
        _thread_id="sender-self", text="Do not write", node_id=foreign.id,
        _host_access_turn_identity=TURN,
    )

    assert result == (
        "Error: node_id is not an open or settled node in your plan; omit it or pass a node from plan_read"
    )
    assert deleg_db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_post_update_retry_key_reuses_the_durable_message(deleg_db, monkeypatch):
    from tools.post_update import PostUpdateTool

    broadcast = AsyncMock()
    monkeypatch.setattr("tools.post_update.broadcast_message_rows", broadcast)
    tool = PostUpdateTool()
    first = await tool.execute(
        _thread_id="sender-self", text="Saved once", idempotency_key="update-1",
        _host_access_turn_identity=TURN,
    )
    second = await tool.execute(
        _thread_id="sender-self", text="Saved once", idempotency_key="update-1",
        _host_access_turn_identity=TURN,
    )

    assert first == second
    assert deleg_db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    broadcast.assert_awaited_once()


def test_post_update_catalog_summary_stays_short_while_description_keeps_guidance():
    from tools.catalog_entries import POST_UPDATE_ATTENTION_GUIDANCE, POST_UPDATE_SUMMARY, TOOL_CATALOG
    from tools.post_update import PostUpdateTool

    summary = TOOL_CATALOG["post_update"].summary
    description = PostUpdateTool().description

    assert summary == POST_UPDATE_SUMMARY
    assert "\n" not in summary
    assert POST_UPDATE_ATTENTION_GUIDANCE in description
    assert description.startswith(summary + ". ")


def test_post_update_reaches_native_claude_code_and_codex(tmp_path):
    from subagents.codex_app_server_events import to_dynamic_tool_specs
    from subagents.harness_tool_bridge import build_tool_registry, dynamic_tool_definitions
    from subagents.corsia_mcp_server import mcp_tool_payloads
    from tools.catalog import pack_members

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = build_tool_registry(str(workspace))
    external = dynamic_tool_definitions(registry)
    cc = {tool["name"] for tool in mcp_tool_payloads(external)}
    codex = {tool["name"] for tool in to_dynamic_tool_specs(external)}

    assert "post_update" in registry.tool_names
    assert "post_update" in pack_members()["history"]
    assert {"tools_load", "tool_call"} <= cc
    assert {"tools_load", "tool_call"} <= codex
