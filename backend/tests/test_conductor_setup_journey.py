"""Mock-mode native dispatch journey through existing creation and roster owners."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from delegation_fixtures import deleg_db  # noqa: F401
from infra.conductor import set_conductor
from tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_fresh_workspace_conductor_discovers_creates_and_reads_agent(deleg_db, tmp_path, monkeypatch):
    import infra.config as config
    from core.agent import _register_agent_roster_tools
    from core.skills_registry import skill_context_sources
    from infra import state
    from tools.delegation import DelegateCreateThreadTool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    assert set_conductor("workspace", "sender-self", db=deleg_db)
    registry = ToolRegistry()
    _register_agent_roster_tools(SimpleNamespace(tools=registry))
    registry.register(DelegateCreateThreadTool())
    skills = skill_context_sources(thread_id="sender-self", project_id=None,
                                    workspace=workspace, db=deleg_db)
    index = next(row for row in skills if row.source_id == "dynamic:skills-index")
    assert "conductor-api" in index.prompt_content
    assert "conductor-api" in registry.get("corsia_api").description
    assert registry.get("coordination_instruction_file_create") is not None

    binding = state._current_thread_id.set("sender-self")
    try:
        before = json.loads(await registry.execute("corsia_api", {"method": "GET", "path": "/api/agents"}))
        assert before["status"] == 200
        created = json.loads(await registry.execute("delegate_create_thread", {
            "title": "Checks", "model": "anthropic/claude-3-5-sonnet", "thread_kind": "chat",
        }))
        assert created["ok"], created
        target = created["thread_id"]
        entry = json.loads(await registry.execute("agent_roster_entry", {"thread_id": target}))
        promotion = {"thread_id": target, "expected_updated_at": entry["updated_at"],
                     "responsibility": "Owns the authorized checks.", "agent_enabled": True}
        promoted = json.loads(await registry.execute("agent_roster_update", promotion))
        assert promoted["changed"]
        retried = json.loads(await registry.execute("agent_roster_update", promotion))
        assert not retried["changed"]
        installed = json.loads(await registry.execute("coordination_instruction_file_create", {
            "target_thread_id": target, "kind": "responsibility", "expected_revision": "absent",
            "content": "# Responsibility\nOwns the authorized checks.\n",
        }))
        assert installed["ok"] and installed["delivery"]["next_turn"], installed
        after = json.loads(await registry.execute("corsia_api", {"method": "GET", "path": "/api/agents"}))
        assert any(row["thread_id"] == target for row in after["data"]["agents"])
        failed = await registry.execute("delegate_create_thread", {"model": "missing/model"})
        assert failed.startswith("Error:"), failed
        denied = await registry.execute("corsia_api", {"method": "POST", "path": "/api/auth/login", "body": {}})
        assert "off-limits" in denied
    finally:
        state._current_thread_id.reset(binding)
