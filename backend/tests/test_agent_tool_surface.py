from __future__ import annotations

import pytest


_PROCESS_TOOLS = {
    "managed_process_status",
    "managed_process_logs",
    "managed_process_stop",
    "process_discover",
    "process_signal",
}
_PLAN_TOOLS = {"plan_read", "plan_ensure", "plan_update", "plan_decision"}
# Advertised with full schemas on every harness; every other tool is reached
# through tools_load/tool_call (#1857).
_ADVERTISED_PLAN_TOOLS = {"plan_read", "plan_update"}


def _loaded_schemas(registry, pack):
    """Return the schemas ``tools_load`` hands every harness for one pack."""
    import asyncio
    import json

    result = asyncio.run(registry.get("tools_load").execute(pack=pack))
    return {item["name"]: item for item in json.loads(result.split("\n", 1)[1])}


def test_external_harness_surface_is_direct_and_cataloged(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    from subagents.harness_tool_bridge import (
        build_tool_registry,
        dynamic_tool_definitions,
    )
    from tools.catalog import missing_catalog_entries, stale_catalog_entries

    # The two claude-code delegation tools register only when that connector is
    # available; assert the complete surface deterministically instead of
    # depending on ambient-DB connection rows left by earlier tests.
    monkeypatch.setattr(
        "connections.models.connection_available",
        lambda provider, db=None: provider == "claude-code",
    )
    from tools.catalog import pack_members

    registry = build_tool_registry(str(workspace))
    names = {
        definition["function"]["name"]
        for definition in dynamic_tool_definitions(registry)
    }
    reachable = {name for pack in pack_members().values() for name in pack}

    assert "corsia_tool" not in names | reachable
    assert "spawn_subagent" not in names | reachable
    assert {"read_file", "edit_file", "tools_load", "tool_call"}.issubset(names)
    assert {"delegate_task", "delegate_send", "agent_roster"}.issubset(reachable)
    assert {
        "doc_comment_create",
        "artifact_resolve",
        "artifact_list",
        "project_create",
        "worktree_create",
    }.issubset(reachable)
    assert missing_catalog_entries(registry.tool_names) == []
    assert stale_catalog_entries(registry.tool_names) == []


def test_agent_roster_tool_has_no_scope_parameters_and_reaches_every_harness(tmp_path):
    from subagents.harness_tool_bridge import build_tool_registry

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = build_tool_registry(str(workspace))
    loaded = _loaded_schemas(registry, "delegation")

    # Every harness loads the same registry schema through tools_load.
    assert loaded["agent_roster"]["parameters"] == {
        "type": "object", "properties": {}, "additionalProperties": False,
    }
    assert loaded["agent_roster"] == registry.get("agent_roster").to_schema()["function"]


@pytest.mark.asyncio
async def test_native_cc_and_codex_share_canonical_plan_surface(tmp_path, monkeypatch):
    """Native must consume the same cataloged definitions as external harnesses."""
    from types import SimpleNamespace

    from core import turn_context
    from subagents.codex_app_server_events import to_dynamic_tool_specs
    from subagents.harness_tool_bridge import (
        build_tool_registry,
        dynamic_tool_definitions,
    )
    from subagents.corsia_mcp_server import mcp_tool_payloads
    from tools.base import Tool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = build_tool_registry(str(workspace))

    class LegacyUpdatePlan(Tool):
        @property
        def name(self):
            return "update_plan"

        @property
        def description(self):
            return "Legacy non-durable provider plan surface."

        @property
        def parameters(self):
            return {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            return "{}"

    registry.register(LegacyUpdatePlan())
    agent = SimpleNamespace(tools=registry)

    async def connect_mcp():
        return None

    agent._connect_mcp = connect_mcp

    async def get_agent():
        return agent

    captured = {}

    async def prepare_turn_context(*args, tool_definitions, **kwargs):
        captured["definitions"] = tool_definitions
        return ([{"role": "user", "content": "hello"}], {})

    monkeypatch.setattr("core.agent.get_agent", get_agent)
    monkeypatch.setattr(turn_context, "prepare_turn_context", prepare_turn_context)

    payload = await turn_context.prepare_native_context_payload(
        thread_id="thread-1",
        user_content="hello",
        target=SimpleNamespace(
            transport_model="test-model",
            context_window=1000,
            catalog_id="test-model",
        ),
        project_id=None,
        media=None,
        replay_selection=None,
    )

    native = list(payload.tool_definitions)
    canonical = dynamic_tool_definitions(registry)
    cc = mcp_tool_payloads(canonical)
    codex = to_dynamic_tool_specs(canonical)

    native_by_name = {item["function"]["name"]: item for item in native}
    canonical_by_name = {item["function"]["name"]: item for item in canonical}
    cc_by_name = {item["name"]: item["inputSchema"] for item in cc}
    codex_by_name = {item["name"]: item["inputSchema"] for item in codex}

    assert native_by_name == canonical_by_name
    assert _ADVERTISED_PLAN_TOOLS <= native_by_name.keys()
    from tools.catalog import pack_members

    assert _PLAN_TOOLS - _ADVERTISED_PLAN_TOOLS <= set(pack_members()["plans"])
    assert "update_plan" not in native_by_name
    assert {
        name: item["function"]["parameters"]
        for name, item in canonical_by_name.items()
    } == cc_by_name == codex_by_name
    assert list(captured["definitions"]) == native


def test_plan_read_params_are_identical_across_native_cc_and_codex(tmp_path):
    """Extends the canonical-surface parity test to the plan read parameters."""
    from subagents.codex_app_server_events import to_dynamic_tool_specs
    from subagents.harness_tool_bridge import (
        build_tool_registry,
        dynamic_tool_definitions,
    )
    from subagents.corsia_mcp_server import mcp_tool_payloads

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = build_tool_registry(str(workspace))
    canonical = dynamic_tool_definitions(registry)
    cc = mcp_tool_payloads(canonical)
    codex = to_dynamic_tool_specs(canonical)

    canonical_by_name = {item["function"]["name"]: item["function"] for item in canonical}
    cc_by_name = {item["name"]: item["inputSchema"] for item in cc}
    codex_by_name = {item["name"]: item["inputSchema"] for item in codex}

    loaded = _loaded_schemas(registry, "plans")
    for name in ("plan_read", "plan_ensure"):
        native_schema = (
            canonical_by_name[name] if name in canonical_by_name else loaded[name]
        )["parameters"]
        assert native_schema["properties"]["mode"]["enum"] == ["status", "full"]
        assert native_schema["properties"]["known_revision"]["type"] == "integer"
        assert not native_schema.get("required")
    assert canonical_by_name["plan_read"]["parameters"] == cc_by_name["plan_read"]
    assert cc_by_name["plan_read"] == codex_by_name["plan_read"]


def test_process_tools_reach_native_cc_and_codex_surfaces(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    from subagents.codex_app_server_events import to_dynamic_tool_specs
    from subagents.harness_tool_bridge import (
        build_tool_registry,
        dynamic_tool_definitions,
    )
    from subagents.corsia_mcp_server import mcp_tool_payloads

    registry = build_tool_registry(str(workspace))
    external = dynamic_tool_definitions(registry)
    native_names = set(registry.tool_names)
    cc_names = {
        definition["name"]
        for definition in mcp_tool_payloads(external)
    }
    codex_names = {
        definition["name"]
        for definition in to_dynamic_tool_specs(external)
    }

    # Every harness reaches deferred tools through the same two advertised tools.
    assert {"tools_load", "tool_call"} <= cc_names
    assert {"tools_load", "tool_call"} <= codex_names
    assert _PROCESS_TOOLS <= native_names
    assert _PROCESS_TOOLS <= set(_loaded_schemas(registry, "host"))
    assert "worktree_create" in native_names
    assert "worktree_create" in _loaded_schemas(registry, "projects")


def test_mcp_payloads_expose_registry_tools_without_bridge_wrapper(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    from subagents.harness_tool_bridge import (
        build_tool_registry,
        dynamic_tool_definitions,
    )
    from subagents.corsia_mcp_server import mcp_tool_payloads

    registry = build_tool_registry(str(workspace))
    payloads = mcp_tool_payloads(dynamic_tool_definitions(registry))
    by_name = {tool["name"]: tool for tool in payloads}

    loaded = _loaded_schemas(registry, "delegation")

    assert "corsia_tool" not in by_name
    assert "read_file" in by_name
    assert "delegate_task" in loaded
    assert by_name["read_file"]["inputSchema"]["required"] == ["path"]
    assert by_name["tool_call"]["inputSchema"]["required"] == ["name", "arguments"]
    assert "target_scope_id" in loaded["delegate_send"]["parameters"]["properties"]


def test_edit_file_public_schema_uses_old_string_new_string_only():
    from tools.filesystem_edit import EditFileTool

    tool = EditFileTool()
    props = tool.parameters["properties"]

    assert "old_string" in props
    assert "new_string" in props
    assert "old_text" not in props
    assert "new_text" not in props
    assert tool.validate_params({
        "path": "app.py",
        "old_string": "a",
        "new_string": "b",
    }) == []
    errors = tool.validate_params({
        "path": "app.py",
        "old_text": "a",
        "new_text": "b",
    })
    assert any("unexpected old_text" in err for err in errors)
    assert any("missing required old_string" in err for err in errors)


def test_multi_edit_rejects_empty_edit_list():
    from tools.filesystem_edit import MultiEditTool

    errors = MultiEditTool().validate_params({"path": "app.py", "edits": []})

    assert "edits must have at least 1 item(s)" in errors


@pytest.mark.asyncio
async def test_multi_edit_is_atomic(tmp_path, monkeypatch):
    from tools import sandbox_config
    from tools.filesystem_edit import MultiEditTool

    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [tmp_path.resolve()])
    monkeypatch.setattr(
        sandbox_config,
        "_list_authoritative_repo_policies",
        lambda: [],
    )
    target = tmp_path / "demo.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    tool = MultiEditTool(workspace=tmp_path)

    result = await tool.execute(
        path="demo.txt",
        edits=[
            {"old_string": "alpha", "new_string": "ALPHA"},
            {"old_string": "missing", "new_string": "MISSING"},
        ],
    )

    assert result.startswith("Error:")
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\n"


@pytest.mark.asyncio
async def test_delegate_task_native_model_uses_delegation_substrate(monkeypatch):
    from tools.delegate_task import DelegateTaskTool

    created: dict = {}
    sent: dict = {}

    async def _create(**kwargs):
        created.update(kwargs)
        return {
            "id": "child-1",
            "user_id": "u-test",
            "profile": "personal",
            "title": kwargs["title"],
            "model": kwargs["model"],
            "project_id": kwargs["project_id"],
            "spawned_by": kwargs["spawned_by"],
        }

    async def _send(thread_id, thread_row, message, **kwargs):
        sent.update({
            "thread_id": thread_id,
            "message": message,
            "kwargs": kwargs,
        })
        return type("SendResult", (), {"driver": "turn_engine", "duplicate": False})()

    monkeypatch.setattr(
        "tools.delegation._current_thread_context",
        lambda sid: {
            "id": sid,
            "user_id": "u-test",
            "profile": "personal",
            "project_id": "proj-1",
            "title": "Parent",
        },
    )
    monkeypatch.setattr("tools.delegate_task.create_visible_fresh_thread", _create)
    monkeypatch.setattr("tools.delegate_task.send_thread_input", _send)
    monkeypatch.setattr(
        "tools.delegate_task.eligible_delegation_model", lambda model, **_scope: (model, None)
    )

    result = await DelegateTaskTool().execute(
        task="do the work",
        title="Worker",
        model="omlx@local/qwen",
        _thread_id="parent-1",
    )

    assert "child-1" in result
    assert created["thread_kind"] == "subagent"
    assert created["spawned_by"] == "parent-1"
    assert created["model"] == "omlx@local/qwen"
    assert sent["thread_id"] == "child-1"
    assert sent["message"] == "do the work"
    assert sent["kwargs"]["metadata"]["sender_kind"] == "agent"
